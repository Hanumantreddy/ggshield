import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import click
import marshmallow_dataclass
from marshmallow import ValidationError

from ggshield.core.config.errors import ParseError, format_validation_error
from ggshield.core.config.utils import (
    get_global_path,
    load_yaml,
    remove_common_dict_items,
    save_yaml,
    update_from_other_instance,
)
from ggshield.core.constants import (
    DEFAULT_LOCAL_CONFIG_PATH,
    GLOBAL_CONFIG_FILENAMES,
    LOCAL_CONFIG_PATHS,
)
from ggshield.core.text_utils import display_warning
from ggshield.core.types import IgnoredMatch, post_init_ignored_match
from ggshield.core.utils import api_to_dashboard_url


CURRENT_CONFIG_VERSION = 2


@dataclass
class SecretConfig:
    """
    Holds all user-defined secret-specific settings
    """

    show_secrets: bool = False
    ignored_detectors: Set[str] = field(default_factory=set)
    ignored_matches: List[IgnoredMatch] = field(default_factory=list)
    ignored_paths: Set[str] = field(default_factory=set)

    def add_ignored_match(self, secret: IgnoredMatch) -> None:
        """
        Add secret to ignored_matches.
        """
        for match in self.ignored_matches:
            if match["match"] == secret["match"]:
                # take the opportunity to name the ignored match
                if not match["name"]:
                    match["name"] = secret["name"]
                return
        self.ignored_matches.append(secret)

    def __post_init__(self) -> None:
        for match in self.ignored_matches:
            post_init_ignored_match(match)


@dataclass
class IaCConfig:
    """
    Holds the iac config as defined .gitguardian.yaml files
    (local and global).
    """

    ignored_paths: Set[str] = field(default_factory=set)
    ignored_policies: Set[str] = field(default_factory=set)
    minimum_severity: str = "LOW"


IaCConfigSchema = marshmallow_dataclass.class_schema(IaCConfig)


@dataclass
class UserConfig:
    """
    Holds all ggshield settings defined by the user in the .gitguardian.yaml files
    (local and global).
    """

    iac: IaCConfig = field(default_factory=IaCConfig)
    instance: Optional[str] = None
    exit_zero: bool = False
    verbose: bool = False
    allow_self_signed: bool = False
    max_commits_for_hook: int = 50
    secret: SecretConfig = field(default_factory=SecretConfig)

    def save(self, config_path: str) -> None:
        """
        Save config to config_path
        """
        schema = UserConfigSchema()
        dct = schema.dump(self)
        default_dct = schema.dump(schema.load({}))

        dct = remove_common_dict_items(dct, default_dct)

        dct["version"] = CURRENT_CONFIG_VERSION

        save_yaml(dct, config_path)

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> Tuple["UserConfig", str]:
        """
        Load the various user configs files to create a UserConfig object:
        - global user configuration file (in the home)
        - local user configuration file (in the repository)

        Returns a UserConfig instance, and the path where updates should be saved
        """

        user_config = UserConfig()
        if config_path:
            user_config._update_from_file(config_path)
            return user_config, config_path

        for global_config_filename in GLOBAL_CONFIG_FILENAMES:
            global_config_path = get_global_path(global_config_filename)
            if os.path.exists(global_config_path):
                user_config._update_from_file(global_config_path)
                break

        for local_config_path in LOCAL_CONFIG_PATHS:
            if os.path.exists(local_config_path):
                user_config._update_from_file(local_config_path)
                config_path = local_config_path
                break

        if config_path is None:
            config_path = DEFAULT_LOCAL_CONFIG_PATH
        return user_config, config_path

    def _update_from_file(self, config_path: str) -> None:
        data = load_yaml(config_path) or {"version": CURRENT_CONFIG_VERSION}
        config_version = data.pop("version", 1)

        try:
            if config_version == 2:
                obj = UserConfigSchema().load(data)
            elif config_version == 1:
                display_warning(
                    f"{config_path} uses a deprecated configuration file format."
                    " Follow the instructions from ggshield README.md to update it."
                )
                obj = UserV1Config.load_v1(data)
            else:
                raise click.ClickException(
                    f"Don't know how to load config version {config_version}"
                )
        except ValidationError as exc:
            message = format_validation_error(exc)
            raise ParseError(f"Error in {config_path}:\n{message}") from exc

        update_from_other_instance(self, obj)


UserConfigSchema = marshmallow_dataclass.class_schema(UserConfig)


@dataclass
class UserV1Config:
    """
    Can load a v1 .gitguardian.yaml file
    """

    instance: Optional[str] = None
    all_policies: bool = False
    exit_zero: bool = False
    matches_ignore: List[IgnoredMatch] = field(default_factory=list)
    paths_ignore: Set[str] = field(default_factory=set)
    verbose: bool = False
    allow_self_signed: bool = False
    max_commits_for_hook: int = 50
    ignore_default_excludes: bool = False
    show_secrets: bool = False
    banlisted_detectors: Set[str] = field(default_factory=set)

    @staticmethod
    def load_v1(data: Dict[str, Any]) -> UserConfig:
        """
        Takes a dict representing a v1 .gitguardian.yaml and returns a v2 config object
        """
        # If data contains the old "api-url" key, turn it into an "instance" key,
        # but only if there is no "instance" key
        try:
            api_url = data.pop("api_url")
        except KeyError:
            pass
        else:
            if "instance" not in data:
                data["instance"] = api_to_dashboard_url(api_url, warn=True)

        UserV1Config.update_matches_ignore(data)

        v1config = UserV1ConfigSchema().load(data)

        if v1config.all_policies:
            display_warning(
                "The `all_policies` option has been deprecated and is now ignored."
            )

        if v1config.ignore_default_excludes:
            display_warning(
                "The `ignore_default_exclude` option has been deprecated and is now ignored."
            )

        secret = SecretConfig(
            show_secrets=v1config.show_secrets,
            ignored_detectors=v1config.banlisted_detectors,
            ignored_matches=v1config.matches_ignore,
            ignored_paths=v1config.paths_ignore,
        )

        return UserConfig(
            instance=v1config.instance,
            exit_zero=v1config.exit_zero,
            verbose=v1config.verbose,
            allow_self_signed=v1config.allow_self_signed,
            max_commits_for_hook=v1config.max_commits_for_hook,
            secret=secret,
        )

    @staticmethod
    def update_matches_ignore(data: Dict[str, Any]) -> None:
        """
        v1 config format allowed to use just a hash of the secret for matches_ignore
        field v2 does not. This function converts the hash-only matches.
        """
        matches_ignore = data.get("matches_ignore")
        if not matches_ignore:
            return

        for idx, match in enumerate(matches_ignore):
            if isinstance(match, str):
                matches_ignore[idx] = {"name": "", "match": match}


UserV1ConfigSchema = marshmallow_dataclass.class_schema(UserV1Config)
