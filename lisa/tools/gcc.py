# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import re
from typing import cast

from semver import VersionInfo

from lisa.executable import Tool
from lisa.operating_system import Posix
from lisa.util import LisaException


class Gcc(Tool):
    # gcc (Ubuntu 11.2.0-19ubuntu1) 11.2.0
    # gcc (GCC) 8.5.0 20210514 (Red Hat 8.5.0-10)
    _version_pattern = re.compile(
        r"gcc \(.*\) (?P<major>\d+).(?P<minor>(\d+)).(?P<patch>(\d+))", re.M
    )

    @property
    def command(self) -> str:
        return "gcc"

    @property
    def can_install(self) -> bool:
        return True

    def compile(self, filename: str, output_name: str = "") -> None:
        if output_name:
            self.run(f"{filename} -o {output_name}")
        else:
            self.run(filename)

    def get_version(self) -> VersionInfo:
        output = self.run(
            "--version",
            expected_exit_code=0,
            expected_exit_code_failure_message="fail to get gcc dumpversion",
        ).stdout
        matched_version = self._version_pattern.match(output)
        if matched_version:
            major = matched_version.group("major")
            minor = matched_version.group("minor")
            patch = matched_version.group("patch")
            return VersionInfo(int(major), int(minor), int(patch))
        raise LisaException("fail to get gcc version")

    def _install(self) -> bool:
        posix_os: Posix = cast(Posix, self.node.os)
        posix_os.install_packages("gcc")
        return self._check_exists()
