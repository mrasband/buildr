#!/usr/bin/env python
import argparse
import logging
import os
import pathlib
import sys
from typing import Dict, Any

import yaml
from docker import Client


logging.basicConfig()
logger = logging.getLogger('buildr')
logger.setLevel(logging.DEBUG)


class BuildError(Exception):
    """Defines an error during the process, this doesn't mean
    anything failed but instead that either a precondition failed
    or a pre-build item."""


class BuildFailure(Exception):
    """Defines a failure in a build step,"""


class ManifestV1:
    """Required properties for a version 1 manifest"""
    def __init__(self, manifest_def):
        self._def = manifest_def

        self._stages = None  # type: Optional[List]
        self._env = None  # type: Optional[List[str]]

    @property
    def stages(self):
        """List of stages to be executed, in order"""
        if self._stages is None:
            self._stages = self._def.get('stages', [])
            if self._def.get('prepare'):
                if 'prepare' in self._stages:
                    self._stages.remove('prepare')
                self._stages.insert(0, 'prepare')
        return self._stages

    @property
    def image(self):
        """Base docker image to run within"""
        return self._def.get('image', 'buildr-ubuntu')

    @property
    def env(self):
        """Prepared environmental variables"""
        if self._env is None:
            self._env = []
            env = self._def.get('environment', {})
            if env.get('inherit', False):
                for e in os.environ.items():
                    self._env.append('='.join(e))
            for var in env.get('vars', []):
                self._env.append(var)
        return self._env

    def __getitem__(self, value):
        """Access stage definitions as dict keys"""
        return self._def.get(value)


def parse_args():
    """Parse the command line arguments"""
    parser = argparse.ArgumentParser(prog='buildr',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)  # noqa
    parser.add_argument('--path', help='Local directory path', type=str,
                        default='.')
    parser.add_argument('--docker-sock', help='Docker sock', type=str,
                        default='unix://var/run/docker.sock')

    return parser.parse_args()


def load_manifest(project_dir: pathlib.Path) -> ManifestV1:
    """Load the manifest from the project directory,
    erroring on the manifest not being found or the
    project directory not existing.
    :param project_dir: Where the project is on disk
    :returns ManifestV1: The parsed manifest"""
    if not project_dir.exists():
        sys.exit('Project directory does not exist')
    logger.debug('Searching for manifest in %s', project_dir)

    buildr = project_dir / '.buildr.yml'
    if not buildr.exists():
        sys.exit('Build definition not found in project.')

    logger.debug('Found manifest, loading...')

    with open(str(buildr.absolute()), 'r') as f:
        manifest = yaml.safe_load(f)
        if manifest.get('version', 1) == 1:
            return ManifestV1(manifest)
        sys.exit('Illegal manifest version')


class Buildr:
    def __init__(self, project_dir: pathlib.Path, *,
                 base_url='unix://var/run/docker.sock', image='docker',
                 env=None):
        self.project_dir = project_dir
        self.base_url = base_url
        self.image = image
        self.env = env
        if env is None:
            self.env = []

        self.cli = None
        self.container_id = None
        self._cm = False

    def __enter__(self):
        self.cli = Client(base_url=self.base_url)
        self.container_id = self._create_container()
        self._start_container()
        self._cm = True

        return self

    def __exit__(self, *args):
        self._close_container()
        self._cm = False

    def execute(self, command: str, writer=sys.stdout.write) -> int:
        """Execute a build command.
        :param command: Command to execute in the shell
        :returns: Exit code"""
        if not self._cm:
            raise ValueError('Buildr must be run as a context manager to'
                             ' ensure all resources are reaped on exit.')

        # You can emulate this with:
        #   docker exec <container_name> <script>
        exec_ = self.cli.exec_create(self.container_id, command)
        exec_id = exec_['Id']

        for chunk in self.cli.exec_start(exec_id, stream=True):
            try:
                writer(chunk.decode())
            except UnicodeDecodeError:
                pass

        result = self.cli.exec_inspect(exec_id)
        return result['ExitCode']

    def _create_container(self):
        """Creates the build runner container"""
        # For debug, you can emulate this with:
        #   docker run -it -v /var/run/docker.sock:/var/run/docker.sock -v "${PWD}":/app \
        #   --workdir /app -e <your envvars> <manifest.image> sh
        container = self.cli.create_container(image=self.image,
                                         command='sh',
                                         detach=True,
                                         environment=self.env,
                                         stdin_open=True,
                                         working_dir='/app',
                                         volumes=[
                                             '/app',
                                             '/var/run/docker.sock',
                                             # '/root/.docker/config.json',  # TODO: remove
                                         ],
                                         host_config=self.cli.create_host_config(binds=[
                                             '{}:/app'.format(self.project_dir.absolute()),  # noqa
                                             '/var/run/docker.sock:/var/run/docker.sock',  # noqa
                                             # TODO: this is temporary, it really should
                                             # probably just be expected to be baked into
                                             # the build image.
                                             # '/root/.docker/config.json:/root/.docker/config.json'  # noqa
                                         ]))
        return container['Id']

    def _start_container(self):
        """Start the container"""
        self.cli.start(self.container_id)

    def _close_container(self):
        """Close, shutdown, and remove the container"""
        self.cli.kill(self.container_id)
        self.cli.remove_container(self.container_id)


def run_manifest(manifest: ManifestV1, target_dir: pathlib.Path, *,
                 docker_sock='unix://var/run/docker.sock',
                 progress_writer=sys.stdout,
                 project_meta=None):
    """Run the manifest stages.

    :param manifest: The manifest to run
    :param target_dir: Which directory the source is in that the manifest
                       targets.
    :param docker_sock: Location of the docker engine socket.
    :param progress_writer: Stream to write echoed stdout from the
                            build container to, defaults to stdout."""
    with Buildr(target_dir, base_url=docker_sock, image=manifest.image,
                env=manifest.env) as buildr:
        try:
            for stage_name in manifest.stages:
                stage = manifest[stage_name]
                for script in stage.get('script', []):
                    rc = buildr.execute(script)
                    if rc != 0:
                        if stage_name == 'prepare':
                            logger.error('Command exited with error, unable to '
                                         'prepare the environment.')
                            raise BuildError('Prepare failed, unable to set up '
                                             'the environment.')
                        else:
                            logger.error('Command exited with error, build '
                                         'failed.')
                            raise BuildFailure('Stage failed.')
        except KeyboardInterrupt:
            print('Run cancelled, exiting')


def main():
    args = parse_args()
    target_dir = pathlib.Path(args.path)
    manifest = load_manifest(target_dir)
    run_manifest(manifest, target_dir, docker_sock=args.docker_sock)


if __name__ == '__main__':
    main()

