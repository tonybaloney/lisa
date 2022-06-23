# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
import re
import time
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Match,
    Optional,
    Pattern,
    Type,
    Union,
)

from assertpy import assert_that
from retry import retry
from semver import VersionInfo

from lisa.base_tools import Cat, Sed, Uname, Wget
from lisa.executable import Tool
from lisa.util import (
    BaseClassMixin,
    LisaException,
    MissingPackagesException,
    filter_ansi_escape,
    find_group_in_lines,
    get_matched_str,
    parse_version,
)
from lisa.util.logger import get_logger
from lisa.util.perf_timer import create_timer
from lisa.util.process import ExecutableResult
from lisa.util.subclasses import Factory

if TYPE_CHECKING:
    from lisa.node import Node


_get_init_logger = partial(get_logger, name="os")


class CpuArchitecture(str, Enum):
    X64 = "x86_64"
    ARM64 = "aarch64"


@dataclass
# stores information about repository in Posix operating systems
class RepositoryInfo(object):
    # name of the repository, for example focal-updates
    name: str


@dataclass
# OsInformation - To have full distro info.
# GetOSVersion() method at below link was useful to get distro info.
# https://github.com/microsoft/lisa/blob/master/Testscripts/Linux/utils.sh
class OsInformation:
    # structured version information, for example 8.0.3
    version: VersionInfo
    # Examples: Microsoft, Red Hat
    vendor: str
    # the string edition of version. Examples: 8.3, 18.04
    release: str = ""
    # Codename for the release
    codename: str = ""
    # Update available
    update: str = ""
    # Full name of release and version. Examples: Ubuntu 18.04.5 LTS (Bionic
    # Beaver), Red Hat Enterprise Linux release 8.3 (Ootpa)
    full_version: str = "Unknown"


@dataclass
# It's similar with UnameResult, and will replace it.
class KernelInformation:
    version: VersionInfo
    raw_version: str
    hardware_platform: str
    operating_system: str
    version_parts: List[str]


class OperatingSystem:
    __lsb_release_pattern = re.compile(r"^Description:[ \t]+([\w]+)[ ]+$", re.M)
    # NAME="Oracle Linux Server"
    __os_release_pattern_name = re.compile(r"^NAME=\"?([^\" \r\n]+).*?\"?\r?$", re.M)
    __os_release_pattern_id = re.compile(r"^ID=\"?([^\" \r\n]+).*?\"?\r?$", re.M)
    # The ID_LIKE is to match some unknown distro, but derived from known distros.
    # For example, the ID and ID_LIKE in /etc/os-release of AlmaLinux is:
    # ID="almalinux"
    # ID_LIKE="rhel centos fedora"
    # The __os_release_pattern_id can match "almalinux"
    # The __os_release_pattern_idlike can match "rhel"
    __os_release_pattern_idlike = re.compile(
        r"^ID_LIKE=\"?([^\" \r\n]+).*?\"?\r?$", re.M
    )
    __redhat_release_pattern_header = re.compile(r"^([^ ]*) .*$")
    # Red Hat Enterprise Linux Server 7.8 (Maipo) => Maipo
    __redhat_release_pattern_bracket = re.compile(r"^.*\(([^ ]*).*\)$")
    __debian_issue_pattern = re.compile(r"^([^ ]+) ?.*$")
    __release_pattern = re.compile(r"^DISTRIB_ID='?([^ \n']+).*$", re.M)
    __suse_release_pattern = re.compile(r"^(SUSE).*$", re.M)

    __posix_factory: Optional[Factory[Any]] = None

    def __init__(self, node: "Node", is_posix: bool) -> None:
        super().__init__()
        self._node: Node = node
        self._is_posix = is_posix
        self._log = get_logger(name="os", parent=self._node.log)
        self._information: Optional[OsInformation] = None
        self._packages: Dict[str, VersionInfo] = dict()

    @classmethod
    def create(cls, node: "Node") -> Any:
        log = _get_init_logger(parent=node.log)
        result: Optional[OperatingSystem] = None

        detected_info = ""
        if node.shell.is_posix:
            # delay create factory to make sure it's late than loading extensions
            if cls.__posix_factory is None:
                cls.__posix_factory = Factory[Posix](Posix)
                cls.__posix_factory.initialize()
            # cast type for easy to use
            posix_factory: Factory[Posix] = cls.__posix_factory

            matched = False
            os_infos: List[str] = []
            for os_info_item in cls._get_detect_string(node):
                if os_info_item:
                    os_infos.append(os_info_item)
                    for sub_type in posix_factory.values():
                        posix_type: Type[Posix] = sub_type
                        pattern = posix_type.name_pattern()
                        if pattern.findall(os_info_item):
                            detected_info = os_info_item
                            result = posix_type(node)
                            matched = True
                            break
                    if matched:
                        break

            if not os_infos:
                raise LisaException(
                    "unknown posix distro, no os info found. "
                    "it may cause by not support basic commands like `cat`"
                )
            elif not result:
                raise LisaException(
                    f"unknown posix distro names '{os_infos}', "
                    f"support it in operating_system."
                )
        else:
            result = Windows(node)
        log.debug(f"detected OS: '{result.name}' by pattern '{detected_info}'")
        return result

    @property
    def is_windows(self) -> bool:
        return not self._is_posix

    @property
    def is_posix(self) -> bool:
        return self._is_posix

    @property
    def information(self) -> OsInformation:
        if not self._information:
            self._information = self._get_information()
            self._log.debug(f"parsed os information: {self._information}")

        return self._information

    @property
    def name(self) -> str:
        return self.__class__.__name__

    def capture_system_information(self, saved_path: Path) -> None:
        ...

    @classmethod
    def _get_detect_string(cls, node: Any) -> Iterable[str]:
        typed_node: Node = node
        cmd_result = typed_node.execute(cmd="lsb_release -d", no_error_log=True)
        yield get_matched_str(cmd_result.stdout, cls.__lsb_release_pattern)

        cmd_result = typed_node.execute(cmd="cat /etc/os-release", no_error_log=True)
        yield get_matched_str(cmd_result.stdout, cls.__os_release_pattern_name)
        yield get_matched_str(cmd_result.stdout, cls.__os_release_pattern_id)
        cmd_result_os_release = cmd_result

        # for RedHat, CentOS 6.x
        cmd_result = typed_node.execute(
            cmd="cat /etc/redhat-release", no_error_log=True
        )
        yield get_matched_str(cmd_result.stdout, cls.__redhat_release_pattern_header)
        yield get_matched_str(cmd_result.stdout, cls.__redhat_release_pattern_bracket)

        # for FreeBSD
        cmd_result = typed_node.execute(cmd="uname", no_error_log=True)
        yield cmd_result.stdout

        # for Debian
        cmd_result = typed_node.execute(cmd="cat /etc/issue", no_error_log=True)
        yield get_matched_str(cmd_result.stdout, cls.__debian_issue_pattern)

        # note, cat /etc/*release doesn't work in some images, so try them one by one
        # try best for other distros, like Sapphire
        cmd_result = typed_node.execute(cmd="cat /etc/release", no_error_log=True)
        yield get_matched_str(cmd_result.stdout, cls.__release_pattern)

        # try best for other distros, like VeloCloud
        cmd_result = typed_node.execute(cmd="cat /etc/lsb-release", no_error_log=True)
        yield get_matched_str(cmd_result.stdout, cls.__release_pattern)

        # try best for some suse derives, like netiq
        cmd_result = typed_node.execute(cmd="cat /etc/SuSE-release", no_error_log=True)
        yield get_matched_str(cmd_result.stdout, cls.__suse_release_pattern)

        # try best from distros'family through ID_LIKE
        yield get_matched_str(
            cmd_result_os_release.stdout, cls.__os_release_pattern_idlike
        )

    def _get_information(self) -> OsInformation:
        raise NotImplementedError()

    def _parse_version(self, version: str) -> VersionInfo:
        return parse_version(version)


class Windows(OperatingSystem):
    # Microsoft Windows [Version 10.0.22000.100]
    __windows_version_pattern = re.compile(
        r"^Microsoft Windows \[Version (?P<version>[0-9.]*?)\]$",
        re.M,
    )

    def __init__(self, node: Any) -> None:
        super().__init__(node, is_posix=False)

    def _get_information(self) -> OsInformation:
        cmd_result = self._node.execute(
            cmd="ver",
            shell=True,
            no_error_log=True,
        )
        cmd_result.assert_exit_code(message="error on get os information:")
        assert cmd_result.stdout, "not found os information from 'ver'"

        full_version = cmd_result.stdout
        version_string = get_matched_str(full_version, self.__windows_version_pattern)
        if not version_string:
            raise LisaException(f"OS version information not found in: {full_version}")

        information = OsInformation(
            version=self._parse_version(version_string),
            vendor="Microsoft",
            release=version_string,
            full_version=full_version,
        )
        return information


class Posix(OperatingSystem, BaseClassMixin):
    _os_info_pattern = re.compile(
        r"^(?P<name>.*)=[\"\']?(?P<value>.*?)[\"\']?$", re.MULTILINE
    )
    # output of /etc/fedora-release - Fedora release 22 (Twenty Two)
    # output of /etc/redhat-release - Scientific Linux release 7.1 (Nitrogen)
    # output of /etc/os-release -
    #   NAME="Debian GNU/Linux"
    #   VERSION_ID="7"
    #   VERSION="7 (wheezy)"
    # output of lsb_release -a
    #   LSB Version:	:base-4.0-amd64:base-4.0-noarch:core-4.0-amd64:core-4.0-noarch
    #   Distributor ID:	Scientific
    #   Description:	Scientific Linux release 6.7 (Carbon)
    # In most of the distros, the text in the brackets is the codename.
    # This regex gets the codename for the distro
    _distro_codename_pattern = re.compile(r"^.*\(([^)]+)")

    def __init__(self, node: Any) -> None:
        super().__init__(node, is_posix=True)
        self._first_time_installation: bool = True

    @classmethod
    def type_name(cls) -> str:
        return cls.__name__

    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile(f"^{cls.type_name()}$")

    def replace_boot_kernel(self, kernel_version: str) -> None:
        raise NotImplementedError("update boot entry is not implemented")

    def get_kernel_information(self, force_run: bool = False) -> KernelInformation:
        uname = self._node.tools[Uname]
        uname_result = uname.get_linux_information(force_run=force_run)

        parts: List[str] = [str(x) for x in uname_result.kernel_version]
        kernel_information = KernelInformation(
            version=uname_result.kernel_version,
            raw_version=uname_result.kernel_version_raw,
            hardware_platform=uname_result.hardware_platform,
            operating_system=uname_result.operating_system,
            version_parts=parts,
        )

        return kernel_information

    def install_packages(
        self,
        packages: Union[str, Tool, Type[Tool], List[Union[str, Tool, Type[Tool]]]],
        signed: bool = False,
        timeout: int = 600,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        package_names = self._get_package_list(packages)
        self._install_packages(package_names, signed, timeout, extra_args)

    def package_exists(self, package: Union[str, Tool, Type[Tool]]) -> bool:
        """
        Query if a package/tool is installed on the node.
        Return Value - bool
        """
        package_name = self.__resolve_package_name(package)
        return self._package_exists(package_name)

    def is_package_in_repo(self, package: Union[str, Tool, Type[Tool]]) -> bool:
        """
        Query if a package/tool exists in the repo
        Return Value - bool
        """
        package_name = self.__resolve_package_name(package)
        return self._is_package_in_repo(package_name)

    def update_packages(
        self, packages: Union[str, Tool, Type[Tool], List[Union[str, Tool, Type[Tool]]]]
    ) -> None:
        package_names = self._get_package_list(packages)
        self._update_packages(package_names)

    def capture_system_information(self, saved_path: Path) -> None:
        # avoid to involve node, it's ok if some command doesn't exist.
        self._node.execute("uname -vrio").save_stdout_to_file(saved_path / "uname.txt")
        self._node.execute(
            "uptime -s || last reboot -F | head -1 | awk '{print $9,$6,$7,$8}'",
            shell=True,
        ).save_stdout_to_file(saved_path / "uptime.txt")
        self._node.execute("modinfo hv_netvsc").save_stdout_to_file(
            saved_path / "modinfo-hv_netvsc.txt"
        )
        try:
            self._node.shell.copy_back(
                self._node.get_pure_path("/etc/os-release"),
                saved_path / "os-release.txt",
            )
        except FileNotFoundError:
            self._log.debug("File /etc/os-release doesn't exist.")

    def get_package_information(
        self, package_name: str, use_cached: bool = True
    ) -> VersionInfo:
        found = self._packages.get(package_name, None)
        if found and use_cached:
            return found
        return self._get_package_information(package_name)

    def get_repositories(self) -> List[RepositoryInfo]:
        raise NotImplementedError("get_repositories is not implemented")

    def _process_extra_package_args(self, extra_args: Optional[List[str]]) -> str:
        if extra_args:
            add_args = " ".join(extra_args)
        else:
            add_args = ""
        return add_args

    def _install_packages(
        self,
        packages: List[str],
        signed: bool = True,
        timeout: int = 600,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        raise NotImplementedError()

    def _update_packages(self, packages: Optional[List[str]] = None) -> None:
        raise NotImplementedError()

    def _package_exists(self, package: str) -> bool:
        raise NotImplementedError()

    def _is_package_in_repo(self, package: str) -> bool:
        raise NotImplementedError()

    def _initialize_package_installation(self) -> None:
        # sub os can override it, but it's optional
        pass

    def _get_package_information(self, package_name: str) -> VersionInfo:
        raise NotImplementedError()

    def _get_version_info_from_named_regex_match(
        self, package_name: str, named_matches: Match[str]
    ) -> VersionInfo:

        essential_matches = ["major", "minor", "build"]

        # verify all essential keys are in our match dict
        assert_that(
            all(map(lambda x: x in named_matches.groupdict().keys(), essential_matches))
        ).described_as(
            "VersionInfo fetch could not identify all required parameters."
        ).is_true()

        # fill in 'patch' version if it's missing
        patch_match = named_matches.group("patch")
        if not patch_match:
            patch_match = "0"
        major_match = named_matches.group("major")
        minor_match = named_matches.group("minor")
        build_match = named_matches.group("build")
        major, minor, patch = map(
            int,
            [major_match, minor_match, patch_match],
        )
        build_match = named_matches.group("build")
        self._node.log.debug(
            f"Found {package_name} version "
            f"{major_match}.{minor_match}.{patch_match}-{build_match}"
        )
        return VersionInfo(major, minor, patch, build=build_match)

    def _cache_and_return_version_info(
        self, package_name: str, info: VersionInfo
    ) -> VersionInfo:
        self._packages[package_name] = info
        return info

    def _get_information(self) -> OsInformation:
        # try to set version info from /etc/os-release.
        cat = self._node.tools[Cat]
        cmd_result = cat.run("/etc/os-release")
        cmd_result.assert_exit_code(message="error on get os information")

        vendor: str = ""
        release: str = ""
        codename: str = ""
        full_version: str = ""
        for row in cmd_result.stdout.splitlines():
            os_release_info = self._os_info_pattern.match(row)
            if not os_release_info:
                continue
            if os_release_info.group("name") == "NAME":
                vendor = os_release_info.group("value")
            elif os_release_info.group("name") == "VERSION_ID":
                release = os_release_info.group("value")
            elif os_release_info.group("name") == "VERSION":
                codename = get_matched_str(
                    os_release_info.group("value"),
                    self._distro_codename_pattern,
                )
            elif os_release_info.group("name") == "PRETTY_NAME":
                full_version = os_release_info.group("value")

        if vendor == "":
            raise LisaException("OS vendor information not found")
        if release == "":
            raise LisaException("OS release information not found")

        information = OsInformation(
            version=self._parse_version(release),
            vendor=vendor,
            release=release,
            codename=codename,
            full_version=full_version,
        )

        return information

    def _get_package_list(
        self, packages: Union[str, Tool, Type[Tool], List[Union[str, Tool, Type[Tool]]]]
    ) -> List[str]:
        package_names: List[str] = []
        if not isinstance(packages, list):
            packages = [packages]

        assert isinstance(packages, list), f"actual:{type(packages)}"
        for item in packages:
            package_names.append(self.__resolve_package_name(item))
        if self._first_time_installation:
            self._first_time_installation = False
            self._initialize_package_installation()
        return package_names

    def _install_package_from_url(
        self,
        package_url: str,
        package_name: str = "",
        signed: bool = True,
        timeout: int = 600,
    ) -> None:
        """
        Used if the package to be installed needs to be downloaded from a url first.
        """
        # when package is URL, download the package first at the working path.
        wget_tool = self._node.tools[Wget]
        pkg = wget_tool.get(package_url, str(self._node.working_path), package_name)
        self.install_packages(pkg, signed, timeout)

    def wait_running_process(self, process_name: str, timeout: int = 5) -> None:
        # by default, wait for 5 minutes
        timeout = 60 * timeout
        timer = create_timer()
        while timeout > timer.elapsed(False):
            cmd_result = self._node.execute(f"pidof {process_name}")
            if cmd_result.exit_code == 1:
                # not found dpkg or zypper process, it's ok to exit.
                break
            time.sleep(1)

        if timeout < timer.elapsed():
            raise Exception(f"timeout to wait previous {process_name} process stop.")

    def __resolve_package_name(self, package: Union[str, Tool, Type[Tool]]) -> str:
        """
        A package can be a string or a tool or a type of tool.
        Resolve it to a standard package_name so it can be installed.
        """
        if isinstance(package, str):
            package_name = package
        elif isinstance(package, Tool):
            package_name = package.package_name
        else:
            assert isinstance(package, type), f"actual:{type(package)}"
            # Create a temp object, it doesn't query.
            # So they can be queried together.
            tool = package.create(self._node)
            package_name = tool.package_name

        return package_name


class BSD(Posix):
    ...


class MacOS(Posix):
    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^Darwin$")


class Linux(Posix):
    ...


class CoreOs(Linux):
    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^coreos|Flatcar|flatcar$")


@dataclass
# `apt-get update` repolist is of the form `<status>:<id> <uri> <name> <metadata>`
# Example:
# Get:5 http://azure.archive.ubuntu.com/ubuntu focal-updates/main amd64 Packages [1298 kB] # noqa: E501
class DebianRepositoryInfo(RepositoryInfo):
    # status for the repository. Examples: `Hit`, `Get`
    status: str

    # id for the repository. Examples : 1, 2
    id: str

    # uri for the repository. Example: `http://azure.archive.ubuntu.com/ubuntu`
    uri: str

    # metadata for the repository. Example: `amd64 Packages [1298 kB]`
    metadata: str


class Debian(Linux):

    # Get:5 http://azure.archive.ubuntu.com/ubuntu focal-updates/main amd64 Packages [1298 kB] # noqa: E501
    _debian_repository_info_pattern = re.compile(
        r"(?P<status>\S+):(?P<id>\d+)\s+(?P<uri>\S+)\s+(?P<name>\S+)"
        r"\s+(?P<metadata>.*)\s*"
    )

    """ Package: dpdk
        Version: 20.11.3-0ubuntu1~backport20.04-202111041420~ubuntu20.04.1
        Version: 1:2.25.1-1ubuntu3.2
    """
    _debian_package_information_regex = re.compile(
        r"Package: ([a-zA-Z0-9:_\-\.]+)\r?\n"  # package name group
        r"Version: ([a-zA-Z0-9:_\-\.~+]+)\r?\n"  # version number group
    )
    _debian_version_splitter_regex = re.compile(
        r"([0-9]+:)?"  # some examples have a mystery number followed by a ':' (git)
        r"(?P<major>[0-9]+)\."  # major
        r"(?P<minor>[0-9]+)\."  # minor
        r"(?P<patch>[0-9]+)"  # patch
        r"-(?P<build>[a-zA-Z0-9-_\.~+]+)"  # build
    )
    # apt-cache policy git
    # git:
    #   Installed: 1:2.17.1-1ubuntu0.9
    #   Candidate: 1:2.17.1-1ubuntu0.9
    #   Version table:
    #  *** 1:2.17.1-1ubuntu0.9 500
    #         500 http://azure.archive.ubuntu.com/ubuntu bionic-updates/main amd64 Packages # noqa: E501
    #         500 http://security.ubuntu.com/ubuntu bionic-security/main amd64 Packages # noqa: E501
    #         100 /var/lib/dpkg/status
    #      1:2.17.0-1ubuntu1 500
    #         500 http://azure.archive.ubuntu.com/ubuntu bionic/main amd64 Packages
    # apt-cache policy mock
    # mock:
    #   Installed: (none)
    #   Candidate: 1.3.2-2
    #   Version table:
    #      1.3.2-2 500
    #         500 http://azure.archive.ubuntu.com/ubuntu bionic/universe amd64 Packages # noqa: E501
    # apt-cache policy test
    # N: Unable to locate package test
    _package_candidate_pattern = re.compile(
        r"([\w\W]*?)(Candidate: \(none\)|Unable to locate package.*)", re.M
    )

    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^debian|Forcepoint|Kali$")

    def get_apt_error(self, stdout: str) -> List[str]:
        error_lines: List[str] = []
        for line in stdout.splitlines(keepends=False):
            if line.startswith("E: "):
                error_lines.append(line)
        return error_lines

    def _get_package_information(self, package_name: str) -> VersionInfo:
        # run update of package info
        apt_info = self._node.execute(
            f"apt show {package_name}",
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not find package information for package {package_name}"
            ),
        )
        match = self._debian_package_information_regex.search(apt_info.stdout)
        if not match:
            raise LisaException(
                "Package information parsing could not find regex match "
                f" for {package_name} using regex "
                f"{self._debian_package_information_regex.pattern}"
            )
        version_str = match.group(2)
        match = self._debian_version_splitter_regex.search(version_str)
        if not match:
            raise LisaException(
                f"Could not parse version info: {version_str} "
                "for package {package_name}"
            )
        self._node.log.debug(f"Attempting to parse version string: {version_str}")
        version_info = self._get_version_info_from_named_regex_match(
            package_name, match
        )
        return self._cache_and_return_version_info(package_name, version_info)

    def wait_running_package_process(self) -> None:
        is_first_time: bool = True
        # wait for 10 minutes
        timeout = 60 * 10
        timer = create_timer()
        while timeout > timer.elapsed(False):

            # fix the dpkg, in case it's broken.
            dpkg_result = self._node.execute(
                "dpkg --force-all --configure -a", sudo=True
            )
            pidof_result = self._node.execute("pidof dpkg dpkg-deb")
            if dpkg_result.exit_code == 0 and pidof_result.exit_code == 1:
                # not found dpkg process, it's ok to exit.
                break
            if is_first_time:
                is_first_time = False
                self._log.debug("found system dpkg process, waiting it...")
            time.sleep(1)

        if timeout < timer.elapsed():
            raise Exception("timeout to wait previous dpkg process stop.")

    def get_repositories(self) -> List[RepositoryInfo]:
        self._initialize_package_installation()
        repo_list_str = self._node.execute("apt-get update", sudo=True).stdout

        repositories: List[RepositoryInfo] = []
        for line in repo_list_str.splitlines():
            matched = self._debian_repository_info_pattern.search(line)
            if matched:
                repositories.append(
                    DebianRepositoryInfo(
                        name=matched.group("name"),
                        status=matched.group("status"),
                        id=matched.group("id"),
                        uri=matched.group("uri"),
                        metadata=matched.group("metadata"),
                    )
                )

        return repositories

    @retry(tries=10, delay=5)
    def add_repository(
        self,
        repo: str,
        no_gpgcheck: bool = True,
        repo_name: Optional[str] = None,
        keys_location: Optional[List[str]] = None,
    ) -> None:
        if keys_location:
            for key_location in keys_location:
                wget = self._node.tools[Wget]
                key_file_path = wget.get(
                    url=key_location,
                    file_path=str(self._node.working_path),
                    force_run=True,
                )
                self._node.execute(
                    cmd=f"apt-key add {key_file_path}",
                    sudo=True,
                    expected_exit_code=0,
                    expected_exit_code_failure_message="fail to add apt key",
                )
        # This command will trigger apt update too, so it doesn't need to update
        # repos again.

        self._node.execute(
            cmd=f'apt-add-repository -y "{repo}"',
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message="fail to add repository",
        )

    @retry(tries=10, delay=5)
    def _initialize_package_installation(self) -> None:
        # wait running system package process.
        self.wait_running_package_process()

        result = self._node.execute("apt-get update", sudo=True)
        result.assert_exit_code(message="\n".join(self.get_apt_error(result.stdout)))

    @retry(tries=30, delay=10)
    def _install_packages(
        self,
        packages: List[str],
        signed: bool = True,
        timeout: int = 600,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        file_packages = []
        for index, package in enumerate(packages):
            if package.endswith(".deb"):
                # If the package is a .deb file then it would first need to be unpacked.
                # using dpkg command before installing it like other packages.
                file_packages.append(package)
                package = Path(package).stem
                packages[index] = package
        add_args = self._process_extra_package_args(extra_args)
        command = (
            f"DEBIAN_FRONTEND=noninteractive apt-get {add_args} "
            f"-y install {' '.join(packages)}"
        )
        if not signed:
            command += " --allow-unauthenticated"
        self.wait_running_package_process()
        if file_packages:
            self._node.execute(
                f"dpkg -i {' '.join(file_packages)}", sudo=True, timeout=timeout
            )
            # after install package, need update the repo
            self._initialize_package_installation()

        install_result = self._node.execute(
            command, shell=True, sudo=True, timeout=timeout
        )
        # get error lines.
        if install_result.exit_code != 0:
            self._initialize_package_installation()
            install_result.assert_exit_code(
                0,
                f"Failed to install {packages}, "
                f"please check the package name and repo are correct or not.\n"
                + "\n".join(self.get_apt_error(install_result.stdout))
                + "\n",
            )

    def _package_exists(self, package: str) -> bool:
        command = "dpkg --get-selections"
        result = self._node.execute(command, sudo=True, shell=True)
        package_pattern = re.compile(f"{package}([ \t]+)install")
        # Not installed package not shown in the output
        # Uninstall package will show as deinstall
        # vim                                             deinstall
        # vim-common                                      install
        if len(list(filter(package_pattern.match, result.stdout.splitlines()))) == 1:
            return True
        return False

    def _is_package_in_repo(self, package: str) -> bool:
        command = f"apt-cache policy {package}"
        result = self._node.execute(command, sudo=True, shell=True)
        matched = get_matched_str(result.stdout, self._package_candidate_pattern)
        if matched:
            return False
        return True

    def _get_information(self) -> OsInformation:
        # try to set version info from /etc/os-release.
        cat = self._node.tools[Cat]
        cmd_result = cat.run("/etc/os-release")
        cmd_result.assert_exit_code(message="error on get os information")

        vendor: str = ""
        release: str = ""
        codename: str = ""
        full_version: str = ""
        for row in cmd_result.stdout.splitlines():
            os_release_info = super()._os_info_pattern.match(row)
            if not os_release_info:
                continue
            if os_release_info.group("name") == "NAME":
                vendor = os_release_info.group("value")
            elif os_release_info.group("name") == "VERSION":
                codename = get_matched_str(
                    os_release_info.group("value"),
                    super()._distro_codename_pattern,
                )
            elif os_release_info.group("name") == "PRETTY_NAME":
                full_version = os_release_info.group("value")

        # version return from /etc/os-release is integer in debian
        # so get the precise version from /etc/debian_version
        # e.g.
        # marketplace image - credativ debian 9-backports 9.20190313.0
        # version from /etc/os-release is 9
        # version from /etc/debian_version is 9.8
        # marketplace image - debian debian-10 10-backports-gen2 0.20210201.535
        # version from /etc/os-release is 10
        # version from /etc/debian_version is 10.7
        cmd_result = cat.run("/etc/debian_version")
        cmd_result.assert_exit_code(message="error on get debian version")
        release = cmd_result.stdout

        if vendor == "":
            raise LisaException("OS vendor information not found")
        if release == "":
            raise LisaException("OS release information not found")

        information = OsInformation(
            version=self._parse_version(release),
            vendor=vendor,
            release=release,
            codename=codename,
            full_version=full_version,
        )

        return information

    def _update_packages(self, packages: Optional[List[str]] = None) -> None:
        command = (
            "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y "
            '-o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" '
        )
        if packages:
            command += " ".join(packages)
        self._node.execute(command, sudo=True, timeout=3600)


class Ubuntu(Debian):
    __lsb_os_info_pattern = re.compile(
        r"^(?P<name>.*):(\s+)(?P<value>.*?)?$", re.MULTILINE
    )
    # gnulinux-5.11.0-1011-azure-advanced-3fdd2548-1430-450b-b16d-9191404598fb
    # prefix: gnulinux
    # postfix: advanced-3fdd2548-1430-450b-b16d-9191404598fb
    __menu_id_parts_pattern = re.compile(
        r"^(?P<prefix>.*?)-.*-(?P<postfix>.*?-.*?-.*?-.*?-.*?-.*?)?$"
    )

    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^Ubuntu|ubuntu$")

    def replace_boot_kernel(self, kernel_version: str) -> None:
        # set installed kernel to default
        #
        # get boot entry id
        # positive example:
        #         menuentry 'Ubuntu, with Linux 5.11.0-1011-azure' --class ubuntu
        # --class gnu-linux --class gnu --class os $menuentry_id_option
        # 'gnulinux-5.11.0-1011-azure-advanced-3fdd2548-1430-450b-b16d-9191404598fb' {
        #
        # negative example:
        #         menuentry 'Ubuntu, with Linux 5.11.0-1011-azure (recovery mode)'
        # --class ubuntu --class gnu-linux --class gnu --class os $menuentry_id_option
        # 'gnulinux-5.11.0-1011-azure-recovery-3fdd2548-1430-450b-b16d-9191404598fb' {
        cat = self._node.tools[Cat]
        menu_id_pattern = re.compile(
            r"^.*?menuentry '.*?(?:"
            + kernel_version
            + r"[^ ]*?)(?<! \(recovery mode\))' "
            r".*?\$menuentry_id_option .*?'(?P<menu_id>.*)'.*$",
            re.M,
        )
        result = cat.run("/boot/grub/grub.cfg", sudo=True)
        submenu_id = get_matched_str(result.stdout, menu_id_pattern)
        assert submenu_id, (
            f"cannot find sub menu id from grub config by pattern: "
            f"{menu_id_pattern.pattern}"
        )
        self._log.debug(f"matched submenu_id: {submenu_id}")

        # get first level menu id in boot menu
        # input is the sub menu id like:
        # gnulinux-5.11.0-1011-azure-advanced-3fdd2548-1430-450b-b16d-9191404598fb
        # output is,
        # gnulinux-advanced-3fdd2548-1430-450b-b16d-9191404598fb
        menu_id = self.__menu_id_parts_pattern.sub(
            r"\g<prefix>-\g<postfix>", submenu_id
        )
        assert menu_id, f"cannot composite menu id from {submenu_id}"

        # composite boot menu in grub
        menu_entry = f"{menu_id}>{submenu_id}"
        self._log.debug(f"composited menu_entry: {menu_entry}")

        self._replace_default_entry(menu_entry)
        self._node.execute("update-grub", sudo=True)

        try:
            # install tool packages
            self.install_packages(
                [
                    f"linux-tools-{kernel_version}-azure",
                    f"linux-cloud-tools-{kernel_version}-azure",
                    f"linux-headers-{kernel_version}-azure",
                ]
            )
        except Exception as identifier:
            self._log.debug(
                f"ignorable error on install packages after replaced kernel: "
                f"{identifier}"
            )

    def _get_information(self) -> OsInformation:
        cmd_result = self._node.execute(
            cmd="lsb_release -a", shell=True, no_error_log=True
        )
        cmd_result.assert_exit_code(message="error on get os information")
        assert cmd_result.stdout, "not found os information from 'lsb_release -a'"

        for row in cmd_result.stdout.splitlines():
            os_release_info = self.__lsb_os_info_pattern.match(row)
            if os_release_info:
                if os_release_info.group("name") == "Distributor ID":
                    vendor = os_release_info.group("value")
                elif os_release_info.group("name") == "Release":
                    release = os_release_info.group("value")
                elif os_release_info.group("name") == "Codename":
                    codename = os_release_info.group("value")
                elif os_release_info.group("name") == "Description":
                    full_version = os_release_info.group("value")

        if vendor == "":
            raise LisaException("OS vendor information not found")
        if release == "":
            raise LisaException("OS release information not found")

        information = OsInformation(
            version=self._parse_version(release),
            vendor=vendor,
            release=release,
            codename=codename,
            full_version=full_version,
        )

        return information

    def _replace_default_entry(self, entry: str) -> None:
        self._log.debug(f"set boot entry to: {entry}")
        sed = self._node.tools[Sed]
        sed.substitute(
            regexp="GRUB_DEFAULT=.*",
            replacement=f"GRUB_DEFAULT='{entry}'",
            file="/etc/default/grub",
            sudo=True,
        )

        # output to log for troubleshooting
        cat = self._node.tools[Cat]
        cat.run("/etc/default/grub")


class FreeBSD(BSD):
    ...


class OpenBSD(BSD):
    ...


@dataclass
# dnf repolist is of the form `<id> <name>`
# Example:
# microsoft-azure-rhel8-eus  Microsoft Azure RPMs for RHEL8 Extended Update Support
class RPMRepositoryInfo(RepositoryInfo):
    # id for the repository, for example: microsoft-azure-rhel8-eus
    id: str


# Linux distros that use RPM.
class RPMDistro(Linux):
    # microsoft-azure-rhel8-eus  Microsoft Azure RPMs for RHEL8 Extended Update Support
    _rpm_repository_info_pattern = re.compile(r"(?P<id>\S+)\s+(?P<name>\S.*\S)\s*")

    # ex: dpdk-20.11-3.el8.x86_64 or dpdk-18.11.8-1.el7_8.x86_64
    _rpm_version_splitter_regex = re.compile(
        r"(?P<package_name>[a-zA-Z0-9\-_]+)-"
        r"(?P<major>[0-9]+)\."
        r"(?P<minor>[0-9]+)\.?"
        r"(?P<patch>[0-9]+)?"
        r"(?P<build>-[a-zA-Z0-9-_\.]+)?"
    )

    def get_repositories(self) -> List[RepositoryInfo]:
        repo_list_str = self._node.execute(
            f"{self._dnf_tool()} repolist", sudo=True
        ).stdout.splitlines()

        # skip to the first entry in the output
        for index, repo_str in enumerate(repo_list_str):
            if repo_str.startswith("repo id"):
                header_index = index
                break
        repo_list_str = repo_list_str[header_index + 1 :]

        repositories: List[RepositoryInfo] = []
        for line in repo_list_str:
            repo_info = self._rpm_repository_info_pattern.search(line)
            if repo_info:
                repositories.append(
                    RPMRepositoryInfo(
                        name=repo_info.group("name"), id=repo_info.group("id")
                    )
                )
        return repositories

    def add_repository(
        self,
        repo: str,
        no_gpgcheck: bool = True,
        repo_name: Optional[str] = None,
        keys_location: Optional[List[str]] = None,
    ) -> None:
        cmd = f'yum-config-manager --add-repo "{repo}"'
        if no_gpgcheck:
            cmd += " --nogpgcheck"
        self._node.execute(
            cmd=cmd,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message="fail to add repository",
        )

    def _get_package_information(self, package_name: str) -> VersionInfo:
        rpm_info = self._node.execute(
            f"rpm -q {package_name}",
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not find package information for package {package_name}"
            ),
        )
        # rpm package should be of format (package_name)-(version)
        matches = self._rpm_version_splitter_regex.search(rpm_info.stdout)
        if not matches:
            raise LisaException(
                f"Could not parse package version {rpm_info} for {package_name}"
            )
        self._node.log.debug(f"Attempting to parse version string: {rpm_info.stdout}")
        version_info = self._get_version_info_from_named_regex_match(
            package_name, matches
        )
        return self._cache_and_return_version_info(package_name, version_info)

    def _install_packages(
        self,
        packages: List[str],
        signed: bool = True,
        timeout: int = 600,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        add_args = self._process_extra_package_args(extra_args)
        command = f"{self._dnf_tool()} install {add_args} -y {' '.join(packages)}"
        if not signed:
            command += " --nogpgcheck"

        install_result = self._node.execute(
            command, shell=True, sudo=True, timeout=timeout
        )
        install_result.assert_exit_code(0, f"Failed to install {packages}.")

        self._log.debug(f"{packages} is/are installed successfully.")

    def _package_exists(self, package: str) -> bool:
        command = f"{self._dnf_tool()} list installed {package}"
        result = self._node.execute(command, sudo=True)
        if result.exit_code == 0:
            for row in result.stdout.splitlines():
                if package in row:
                    return True

        return False

    def _is_package_in_repo(self, package: str) -> bool:
        command = f"{self._dnf_tool()} list {package} -y"
        result = self._node.execute(command, sudo=True, shell=True)
        return 0 == result.exit_code

    def _dnf_tool(self) -> str:
        return "dnf"


class Fedora(RPMDistro):
    # Red Hat Enterprise Linux Server 7.8 (Maipo) => 7.8
    _fedora_release_pattern_version = re.compile(r"^.*release\s+([0-9\.]+).*$")

    # 305.40.1.el8_4.x86_64
    # 240.el8.x86_64
    __kernel_version_parts_pattern = re.compile(
        r"^(?P<part1>\d+)\.(?P<part2>\d+)?\.?(?P<part3>\d+)?\.?"
        r"(?P<distro>.*?)\.(?P<platform>.*?)$"
    )

    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^Fedora|fedora$")

    def get_kernel_information(self, force_run: bool = False) -> KernelInformation:
        kernel_information = super().get_kernel_information(force_run)
        # original parts: version_parts=['4', '18', '0', '305.40.1.el8_4.x86_64', '']
        # target parts: version_parts=['4', '18', '0', '305', '40', '1', 'el8_4',
        #   'x86_64']
        groups = find_group_in_lines(
            kernel_information.version_parts[3], self.__kernel_version_parts_pattern
        )
        new_parts = kernel_information.version_parts[:3]
        # the default '1' is trying to build a meaningful Redhat version number.
        new_parts.extend(
            [
                groups["part1"],
                groups["part2"],
                groups["part3"],
                groups["distro"],
                groups["platform"],
            ]
        )
        for index, part in enumerate(new_parts):
            if part is None:
                new_parts[index] = ""
        kernel_information.version_parts = new_parts

        return kernel_information

    def install_epel(self) -> None:
        # Extra Packages for Enterprise Linux (EPEL) is a special interest group
        # (SIG) from the Fedora Project that provides a set of additional packages
        # for RHEL (and CentOS, and others) from the Fedora sources.

        major = self._node.os.information.version.major
        assert_that(major).described_as(
            "Fedora/RedHat version must be greater than 7"
        ).is_greater_than_or_equal_to(7)
        epel_release_rpm_name = f"epel-release-latest-{major}.noarch.rpm"
        self.install_packages(
            f"https://dl.fedoraproject.org/pub/epel/{epel_release_rpm_name}"
        )

        # replace $releasever to 8 for 8.x
        if major == 8:
            sed = self._node.tools[Sed]
            sed.substitute("$releasever", "8", "/etc/yum.repos.d/epel*.repo", sudo=True)

    def _get_information(self) -> OsInformation:
        cmd_result = self._node.execute(
            # Typical output of 'cat /etc/fedora-release' is -
            # Fedora release 22 (Twenty Two)
            cmd="cat /etc/fedora-release",
            no_error_log=True,
        )

        cmd_result.assert_exit_code(message="error on get os information")
        full_version = cmd_result.stdout
        if "Fedora" not in full_version:
            raise LisaException("OS version information not found")

        vendor = "Fedora"
        release = get_matched_str(full_version, self._fedora_release_pattern_version)
        codename = get_matched_str(full_version, self._distro_codename_pattern)

        information = OsInformation(
            version=self._parse_version(release),
            vendor=vendor,
            release=release,
            codename=codename,
            full_version=full_version,
        )

        return information


class Redhat(Fedora):
    # Red Hat Enterprise Linux Server release 6.9 (Santiago)
    # CentOS release 6.9 (Final)
    # CentOS Linux release 8.3.2011
    __legacy_redhat_information_pattern = re.compile(
        r"^(?P<vendor>.*?)?(?: Enterprise Linux Server)?(?: Linux)?"
        r"(?: release)? (?P<version>[0-9\.]+)(?: \((?P<codename>.*).*\))?$"
    )
    # Oracle Linux Server
    # Red Hat Enterprise Linux Server
    # Red Hat Enterprise Linux
    __vendor_pattern = re.compile(
        r"^(?P<vendor>.*?)?(?: Enterprise)?(?: Linux)?(?: Server)?$"
    )

    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^rhel|Red|AlmaLinux|Rocky|Scientific|acronis|Actifio$")

    def replace_boot_kernel(self, kernel_version: str) -> None:
        # Redhat kernel is replaced when installing RPM. For source code
        # installation, it's implemented in source code installer.
        ...

    def group_install_packages(self, group_name: str) -> None:
        # trigger to run _initialize_package_installation
        self._get_package_list(group_name)
        result = self._node.execute(f'yum -y groupinstall "{group_name}"', sudo=True)
        self.__verify_package_result(result, group_name)

    def capture_system_information(self, saved_path: Path) -> None:
        super().capture_system_information(saved_path)
        self._node.shell.copy_back(
            self._node.get_pure_path("/etc/redhat-release"),
            saved_path / "redhat-release.txt",
        )

    @retry(tries=10, delay=5)
    def _initialize_package_installation(self) -> None:
        information = self._get_information()
        # We may hit issue when run any yum command, caused by out of date
        #  rhui-microsoft-azure-rhel package.
        # Use below command to update rhui-microsoft-azure-rhel package from microsoft
        #  repo to resolve the issue.
        # Details please refer https://docs.microsoft.com/en-us/azure/virtual-machines/workloads/redhat/redhat-rhui#azure-rhui-infrastructure # noqa: E501
        if "Red Hat" == information.vendor:
            cmd_result = self._node.execute(
                "yum update -y --disablerepo='*' --enablerepo='*microsoft*' ", sudo=True
            )
            cmd_result.assert_exit_code()

    def _install_packages(
        self,
        packages: List[str],
        signed: bool = True,
        timeout: int = 600,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        add_args = self._process_extra_package_args(extra_args)
        command = f"yum install {add_args} -y {' '.join(packages)}"
        if not signed:
            command += " --nogpgcheck"

        install_result = self._node.execute(
            command, shell=True, sudo=True, timeout=timeout
        )
        # RedHat will fail package installation is a single missing package is
        # detected, therefore we check the output to see if we were missing
        # a package. If so, fail. Otherwise we will warn in verify package result.
        if install_result.exit_code == 1:
            missing_packages = []
            for line in install_result.stdout.splitlines():
                if line.startswith("No match for argument:"):
                    package = line.split(":")[1].strip()
                    missing_packages.append(package)
            if missing_packages:
                raise MissingPackagesException(missing_packages)
        self.__verify_package_result(install_result, packages)

    def _package_exists(self, package: str) -> bool:
        command = f"yum list installed {package}"
        result = self._node.execute(command, sudo=True)
        if result.exit_code == 0:
            return True

        return False

    def _is_package_in_repo(self, package: str) -> bool:
        if self._first_time_installation:
            self._initialize_package_installation()
            self._first_time_installation = False
        command = f"yum --showduplicates list {package}"
        result = self._node.execute(command, sudo=True, shell=True)
        return 0 == result.exit_code

    def _get_information(self) -> OsInformation:
        # The higher version above 7.0 support os-version.
        try:
            information = super(Fedora, self)._get_information()

            # remove Linux Server in vendor
            information.vendor = get_matched_str(
                information.vendor, self.__vendor_pattern
            )
        except Exception:
            # Parse /etc/redhat-release to support 6.x and 8.x. Refer to
            # examples of __legacy_redhat_information_pattern.
            cmd_result = self._node.execute(
                cmd="cat /etc/redhat-release", no_error_log=True
            )
            cmd_result.assert_exit_code()
            full_version = cmd_result.stdout
            matches = self.__legacy_redhat_information_pattern.match(full_version)
            assert matches, f"cannot match version information from: {full_version}"
            assert matches.group("vendor")
            information = OsInformation(
                version=self._parse_version(matches.group("version")),
                vendor=matches.group("vendor"),
                release=matches.group("version"),
                codename=matches.group("codename"),
                full_version=full_version,
            )

        return information

    def _update_packages(self, packages: Optional[List[str]] = None) -> None:
        command = "yum -y --nogpgcheck update "
        if packages:
            command += " ".join(packages)
        # older images cost much longer time when update packages
        # smaller sizes cost much longer time when update packages, e.g.
        #  Basic_A1, Standard_A5, Standard_A1_v2, Standard_D1
        # redhat rhel 7-lvm 7.7.2019102813 Basic_A1 cost 2371.568 seconds
        # redhat rhel 8.1 8.1.2020020415 Basic_A0 cost 2409.116 seconds
        self._node.execute(command, sudo=True, timeout=3600)

    def __verify_package_result(self, result: ExecutableResult, packages: Any) -> None:
        # yum returns exit_code=1 if DNF handled an error with installation.
        # We do not want to fail if exit_code=1, but warn since something may
        # potentially have gone wrong.
        if result.exit_code == 1:
            self._log.debug(f"DNF handled error with installation of {packages}")
        elif result.exit_code == 0:
            self._log.debug(f"{packages} is/are installed successfully.")
        else:
            raise LisaException(
                f"Failed to install {packages}. exit_code: {result.exit_code}"
            )

    def _dnf_tool(self) -> str:
        return "yum"


class CentOs(Redhat):
    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^CentOS|Centos|centos|clear-linux-os$")

    def capture_system_information(self, saved_path: Path) -> None:
        super(Linux, self).capture_system_information(saved_path)
        self._node.shell.copy_back(
            self._node.get_pure_path("/etc/centos-release"),
            saved_path / "centos-release.txt",
        )

    def _initialize_package_installation(self) -> None:
        information = self._get_information()
        if 8 == information.version.major:
            # refer https://www.centos.org/centos-linux-eol/
            # CentOS 8 is EOL, old repo mirror was moved to vault.centos.org
            # CentOS-AppStream.repo, CentOS-Base.repo may contain non-existed repo
            # use skip_if_unavailable to aviod installation issues bring in by above
            #  issue
            cmd_results = self._node.execute("yum repolist -v", sudo=True)
            if 0 != cmd_results.exit_code:
                cmd_results = self._node.execute(
                    "yum-config-manager --save --setopt=skip_if_unavailable=true",
                    sudo=True,
                )


class Oracle(Redhat):
    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        # The name is "Oracle Linux Server", which doesn't support the default
        # full match.
        return re.compile("^Oracle")


class CBLMariner(RPMDistro):
    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^Common Base Linux Mariner|mariner$")

    def __init__(self, node: Any) -> None:
        super().__init__(node)
        self._dnf_tool_name: str

    def _initialize_package_installation(self) -> None:
        result = self._node.execute("command -v dnf", no_info_log=True, shell=True)
        if result.exit_code == 0:
            self._dnf_tool_name = "dnf"
            return

        self._dnf_tool_name = "tdnf -q"

    def _dnf_tool(self) -> str:
        return self._dnf_tool_name


@dataclass
# `zypper lr` repolist is of the form
# `<id>|<alias>|<name>|<enabled>|<gpg_check>|<refresh>`
# Example:
# # 4 | repo-oss            | Main Repository             | Yes     | (r ) Yes  | Yes
class SuseRepositoryInfo(RepositoryInfo):
    # id for the repository. Example: 4
    id: str

    # alias for the repository. Example: repo-oss
    alias: str

    # is repository enabled. Example: True/False
    enabled: bool

    # is gpg_check enabled. Example: True/False
    gpg_check: bool

    # is repository refreshed. Example: True/False
    refresh: bool


class Suse(Linux):
    # 55 | Web_and_Scripting_Module_x86_64:SLE-Module-Web-Scripting15-SP2-Updates                           | SLE-Module-Web-Scripting15-SP2-Updates                  | Yes     | ( p) Yes  | Yes # noqa: E501
    # 4 | repo-oss            | Main Repository             | Yes     | (r ) Yes  | Yes # noqa: E501
    _zypper_table_entry = re.compile(
        r"\s*(?P<id>\d+)\s+[|]\s+(?P<alias>\S.+\S)\s+\|\s+(?P<name>\S.+\S)\s+\|"
        r"\s+(?P<enabled>\S.*\S)\s+\|\s+(?P<gpg_check>\S.*\S)\s+\|"
        r"\s+(?P<refresh>\S.*\S)\s*"
    )

    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^SUSE|opensuse-leap$")

    def get_repositories(self) -> List[RepositoryInfo]:
        # Parse output of command "zypper lr"
        # Example output:
        # 1 | Basesystem_Module_x86_64:SLE-Module-Basesystem15-SP2-Debuginfo-Pool                              | SLE-Module-Basesystem15-SP2-Debuginfo-Pool              | No      | ----      | ---- # noqa: E501
        # 2 | Basesystem_Module_x86_64:SLE-Module-Basesystem15-SP2-Debuginfo-Updates                           | SLE-Module-Basesystem15-SP2-Debuginfo-Updates           | No      | ----      | ---- # noqa: E501
        self._initialize_package_installation()
        output = filter_ansi_escape(self._node.execute("zypper lr", sudo=True).stdout)
        repo_list: List[RepositoryInfo] = []

        for line in output.splitlines():
            matched = self._zypper_table_entry.search(line)
            if matched:
                is_repository_enabled = (
                    True if "Yes" in matched.group("enabled") else False
                )
                is_gpg_check_enabled = (
                    True if "Yes" in matched.group("gpg_check") else False
                )
                is_repository_refreshed = (
                    True if "Yes" in matched.group("refresh") else False
                )
                if matched:
                    repo_list.append(
                        SuseRepositoryInfo(
                            name=matched.group("name"),
                            id=matched.group("id"),
                            alias=matched.group("alias"),
                            enabled=is_repository_enabled,
                            gpg_check=is_gpg_check_enabled,
                            refresh=is_repository_refreshed,
                        )
                    )
        return repo_list

    def add_repository(
        self,
        repo: str,
        no_gpgcheck: bool = True,
        repo_name: Optional[str] = None,
        keys_location: Optional[List[str]] = None,
    ) -> None:
        cmd = "zypper ar"
        if no_gpgcheck:
            cmd += " -G "
        cmd += f" {repo} {repo_name}"
        cmd_result = self._node.execute(cmd=cmd, sudo=True)
        if "already exists. Please use another alias." not in cmd_result.stdout:
            if cmd_result.exit_code != 0:
                raise LisaException(f"fail to add repo {repo}")
        else:
            self._log.debug(f"repo {repo_name} already exist")

    def _initialize_package_installation(self) -> None:
        self.wait_running_process("zypper")
        self._node.execute(
            "zypper --non-interactive --gpg-auto-import-keys refresh", sudo=True
        )

    def _install_packages(
        self,
        packages: List[str],
        signed: bool = True,
        timeout: int = 600,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        add_args = self._process_extra_package_args(extra_args)
        command = f"zypper --non-interactive {add_args}"
        if not signed:
            command += " --no-gpg-checks "
        command += f" in {' '.join(packages)}"
        self.wait_running_process("zypper")
        install_result = self._node.execute(
            command, shell=True, sudo=True, timeout=timeout
        )
        if install_result.exit_code in (1, 100):
            raise LisaException(
                f"Failed to install {packages}. exit_code: {install_result.exit_code}, "
                f"stderr: {install_result.stderr}"
            )
        elif install_result.exit_code == 0:
            self._log.debug(f"{packages} is/are installed successfully.")
        else:
            self._log.debug(
                f"{packages} is/are installed."
                " A system reboot or package manager restart might be required."
            )

    def _update_packages(self, packages: Optional[List[str]] = None) -> None:
        command = "zypper --non-interactive --gpg-auto-import-keys update "
        if packages:
            command += " ".join(packages)
        self._node.execute(command, sudo=True, timeout=3600)

    def _package_exists(self, package: str) -> bool:
        command = f"zypper search --installed-only --match-exact {package}"
        result = self._node.execute(command, sudo=True, shell=True)
        return 0 == result.exit_code

    def _is_package_in_repo(self, package: str) -> bool:
        command = f"zypper search -s --match-exact {package}"
        result = self._node.execute(command, sudo=True, shell=True)
        return 0 == result.exit_code


class SLES(Suse):
    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        return re.compile("^SLES|sles|sle-hpc|sle_hpc$")


class NixOS(Linux):
    pass


class OtherLinux(Linux):
    @classmethod
    def name_pattern(cls) -> Pattern[str]:
        """
        FMOS - firemon firemon_sip_azure firemon_sip_azure_byol 9.1.3
        idms - linuxbasedsystemsdesignltd1580878904727 idmslinux
               idmslinux_nosla 2020.0703.1
        RecoveryOS - unitrends unitrends-enterprise-backup-azure ueb9-azure-trial 1.0.9
        sinefa - sinefa sinefa-probe sf-va-msa 26.6.3
        """
        return re.compile(
            "^Sapphire|Buildroot|OpenWrt|BloombaseOS|FMOS|idms|RecoveryOS|sinefa$"
        )
