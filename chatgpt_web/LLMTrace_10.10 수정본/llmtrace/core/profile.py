from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Dict, Optional

@dataclass
class SurfaceProfile:
    name: str
    package: str
    profile: ModuleType
    collection: Optional[ModuleType]
    mappings: Optional[ModuleType]
    rules: ModuleType
    context: Optional[ModuleType]
    report: Optional[ModuleType]
    team_share: Optional[ModuleType]
    parsers: Optional[ModuleType]

    @property
    def metadata(self) -> Dict[str, Any]:
        return {
            'profile_id': getattr(self.profile, 'PROFILE_ID', self.name),
            'service': getattr(self.profile, 'SERVICE', ''),
            'surface': getattr(self.profile, 'SURFACE', ''),
            'version': getattr(self.profile, 'PROFILE_VERSION', '0.1'),
            'description': getattr(self.profile, 'DESCRIPTION', ''),
        }


def _optional_import(name: str) -> Optional[ModuleType]:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            return None
        raise


def load_profile(name: str) -> SurfaceProfile:
    package = f'llmtrace.profiles.{name}'
    profile = importlib.import_module(package + '.profile')
    rules = importlib.import_module(package + '.rules')
    return SurfaceProfile(
        name=name,
        package=package,
        profile=profile,
        collection=_optional_import(package + '.collection'),
        mappings=_optional_import(package + '.mappings'),
        rules=rules,
        context=_optional_import(package + '.context'),
        report=_optional_import(package + '.report'),
        team_share=_optional_import(package + '.team_share'),
        parsers=_optional_import(package + '.parsers'),
    )
