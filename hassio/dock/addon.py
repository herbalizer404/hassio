"""Init file for HassIO addon docker object."""
import logging
from pathlib import Path
import shutil

import docker
import requests

from .interface import DockerInterface
from .util import dockerfile_template, docker_process
from ..const import (
    META_ADDON, MAP_CONFIG, MAP_SSL, MAP_ADDONS, MAP_BACKUP, MAP_SHARE)

_LOGGER = logging.getLogger(__name__)

AUDIO_DEVICE = "/dev/snd:/dev/snd:rwm"


class DockerAddon(DockerInterface):
    """Docker hassio wrapper for HomeAssistant."""

    def __init__(self, config, loop, api, addon):
        """Initialize docker homeassistant wrapper."""
        super().__init__(
            config, loop, api, image=addon.image, timeout=addon.timeout)
        self.addon = addon

    @property
    def name(self):
        """Return name of docker container."""
        return "addon_{}".format(self.addon.slug)

    @property
    def hostname(self):
        """Return slug/id of addon."""
        return self.addon.slug.replace('_', '-')

    @property
    def environment(self):
        """Return environment for docker add-on."""
        addon_env = self.addon.environment or {}
        if self.addon.with_audio:
            addon_env.update({
                'ALSA_OUTPUT': self.addon.audio_output,
                'ALSA_INPUT': self.addon.audio_input,
            })

        return {
            **addon_env,
            'TZ': self.config.timezone,
        }

    @property
    def devices(self):
        """Return needed devices."""
        devices = self.addon.devices or []

        # use audio devices
        if self.addon.with_audio and AUDIO_DEVICE not in devices:
            devices.append(AUDIO_DEVICE)

        # Return None if no devices is present
        if devices:
            return devices
        return None

    @property
    def ports(self):
        """Filter None from addon ports."""
        if not self.addon.ports:
            return None

        return {
            container_port: host_port
            for container_port, host_port in self.addon.ports.items()
            if host_port
        }

    @property
    def tmpfs(self):
        """Return tmpfs for docker add-on."""
        options = self.addon.tmpfs
        if options:
            return {"/tmpfs": "{}".format(options)}
        return None

    @property
    def network_mapping(self):
        """Return hosts mapping."""
        return {
            'homeassistant': self.docker.network.gateway,
        }

    @property
    def network_mode(self):
        """Return network mode for addon."""
        if self.addon.host_network:
            return 'host'
        return None

    @property
    def volumes(self):
        """Generate volumes for mappings."""
        volumes = {
            str(self.addon.path_extern_data): {
                'bind': '/data', 'mode': 'rw'
            }}

        addon_mapping = self.addon.map_volumes

        if MAP_CONFIG in addon_mapping:
            volumes.update({
                str(self.config.path_extern_config): {
                    'bind': '/config', 'mode': addon_mapping[MAP_CONFIG]
                }})

        if MAP_SSL in addon_mapping:
            volumes.update({
                str(self.config.path_extern_ssl): {
                    'bind': '/ssl', 'mode': addon_mapping[MAP_SSL]
                }})

        if MAP_ADDONS in addon_mapping:
            volumes.update({
                str(self.config.path_extern_addons_local): {
                    'bind': '/addons', 'mode': addon_mapping[MAP_ADDONS]
                }})

        if MAP_BACKUP in addon_mapping:
            volumes.update({
                str(self.config.path_extern_backup): {
                    'bind': '/backup', 'mode': addon_mapping[MAP_BACKUP]
                }})

        if MAP_SHARE in addon_mapping:
            volumes.update({
                str(self.config.path_extern_share): {
                    'bind': '/share', 'mode': addon_mapping[MAP_SHARE]
                }})

        return volumes

    def _run(self):
        """Run docker image.

        Need run inside executor.
        """
        if self._is_running():
            return True

        # cleanup
        self._stop()

        # write config
        if not self.addon.write_options():
            return False

        ret = self.docker.run(
            self.image,
            name=self.name,
            hostname=self.hostname,
            detach=True,
            network_mode=self.network_mode,
            ports=self.ports,
            extra_hosts=self.network_mapping,
            devices=self.devices,
            cap_add=self.addon.privileged,
            environment=self.environment,
            volumes=self.volumes,
            tmpfs=self.tmpfs
        )

        if ret:
            _LOGGER.info("Start docker addon %s with version %s",
                         self.image, self.version)

        return ret

    def _install(self, tag):
        """Pull docker image or build it.

        Need run inside executor.
        """
        if self.addon.need_build:
            return self._build(tag)

        return super()._install(tag)

    def _build(self, tag):
        """Build a docker container.

        Need run inside executor.
        """
        build_dir = Path(self.config.path_tmp, self.addon.slug)
        try:
            # prepare temporary addon build folder
            try:
                source = self.addon.path_location
                shutil.copytree(str(source), str(build_dir))
            except shutil.Error as err:
                _LOGGER.error("Can't copy %s to temporary build folder -> %s",
                              source, err)
                return False

            # prepare Dockerfile
            try:
                dockerfile_template(
                    Path(build_dir, 'Dockerfile'), self.config.arch,
                    tag, META_ADDON)
            except OSError as err:
                _LOGGER.error("Can't prepare dockerfile -> %s", err)

            # run docker build
            try:
                build_tag = "{}:{}".format(self.image, tag)

                _LOGGER.info("Start build %s on %s", build_tag, build_dir)
                image = self.docker.images.build(
                    path=str(build_dir), tag=build_tag, pull=True,
                    forcerm=True
                )

                image.tag(self.image, tag='latest')
                self.process_metadata(image.attrs, force=True)

            except (docker.errors.DockerException, TypeError) as err:
                _LOGGER.error("Can't build %s -> %s", build_tag, err)
                return False

            _LOGGER.info("Build %s done", build_tag)
            return True

        finally:
            shutil.rmtree(str(build_dir), ignore_errors=True)

    @docker_process
    def export_image(self, path):
        """Export current images into a tar file."""
        return self.loop.run_in_executor(None, self._export_image, path)

    def _export_image(self, tar_file):
        """Export current images into a tar file.

        Need run inside executor.
        """
        try:
            image = self.docker.api.get_image(self.image)
        except docker.errors.DockerException as err:
            _LOGGER.error("Can't fetch image %s -> %s", self.image, err)
            return False

        try:
            with tar_file.open("wb") as write_tar:
                for chunk in image.stream():
                    write_tar.write(chunk)
        except (OSError, requests.exceptions.ReadTimeout) as err:
            _LOGGER.error("Can't write tar file %s -> %s", tar_file, err)
            return False

        _LOGGER.info("Export image %s to %s", self.image, tar_file)
        return True

    @docker_process
    def import_image(self, path, tag):
        """Import a tar file as image."""
        return self.loop.run_in_executor(None, self._import_image, path, tag)

    def _import_image(self, tar_file, tag):
        """Import a tar file as image.

        Need run inside executor.
        """
        try:
            with tar_file.open("rb") as read_tar:
                self.docker.api.load_image(read_tar)

            image = self.docker.images.get(self.image)
            image.tag(self.image, tag=tag)
        except (docker.errors.DockerException, OSError) as err:
            _LOGGER.error("Can't import image %s -> %s", self.image, err)
            return False

        _LOGGER.info("Import image %s and tag %s", tar_file, tag)
        self.process_metadata(image.attrs, force=True)
        self._cleanup()
        return True

    def _restart(self):
        """Restart docker container.

        Addons prepare some thing on start and that is normaly not repeatable.
        Need run inside executor.
        """
        self._stop()
        return self._run()
