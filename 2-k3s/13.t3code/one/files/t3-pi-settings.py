#!/usr/bin/env python3
"""Migrate T3 provider settings to Pi while preserving a rollback copy."""
import json
from pathlib import Path
import sys
import os
import tempfile


MODEL = 'cliproxy/claude/claude-opus-5-5'


def migrate(home, base_url):
    path = home / 'userdata/settings.json'
    if path.exists():
        settings = json.loads(path.read_text())
    else:
        settings = {
            'providerInstances': {
                # cliproxy force-model-prefix rejects Claude Code's bare model names.
                'claudeAgent': {'driver': 'claudeAgent', 'displayName': 'Claude (via cliproxy)', 'enabled': False},
                'codex': {'driver': 'codex', 'displayName': 'Codex (via cliproxy)', 'enabled': True,
                          'config': {
                              'launchArgs': '-c model_providers.cliproxy.name="cliproxy" '
                                            f'-c model_providers.cliproxy.base_url="{base_url}/v1" '
                                            '-c model_providers.cliproxy.env_key="ANTHROPIC_AUTH_TOKEN" '
                                            '-c model_providers.cliproxy.wire_api="responses" '
                                            '-c model_provider="cliproxy"',
                              'customModels': ['codex/codex-auto-review']}}},
            'defaultModelSelection': {'instanceId': 'pi', 'model': MODEL},
            'textGenerationModelSelection': {'instanceId': 'pi', 'model': MODEL}}
    before = json.dumps(settings, sort_keys=True)
    instances = settings.setdefault('providerInstances', {})
    migrating = 'opencode' in instances
    backup = path.with_name('settings.json.before-pi')
    if migrating and not backup.exists():
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.before-pi-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(path.read_bytes())
            os.link(tmp, backup)
        finally:
            Path(tmp).unlink(missing_ok=True)
    instances.pop('opencode', None)
    settings.get('providers', {}).pop('opencode', None)
    instance = instances.setdefault('pi', {'displayName': 'Pi', 'enabled': True})
    if migrating:
        instance.update(enabled=True, displayName='Pi')
    instance['driver'] = 'pi'
    instance.setdefault('config', {})['binaryPath'] = '/scripts/pi.sh'
    for key in ['defaultModelSelection', 'textGenerationModelSelection']:
        selection = settings.get(key)
        if isinstance(selection, dict) and selection.get('instanceId') == 'opencode':
            selection.update(instanceId='pi', model=MODEL)
            if 'options' in selection:
                selection['options'] = [
                    {**option, 'id': 'thinking'} if isinstance(option, dict) and option.get('id') == 'variant' else option
                    for option in selection['options']
                    if not isinstance(option, dict) or option.get('id') != 'variant' or option.get('value') in
                    ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or json.dumps(settings, sort_keys=True) != before:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.t3-pi-settings-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(settings, stream, indent=2)
                stream.write('\n')
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    marker = path.parent / '.pi-cache-reset'
    if not marker.exists():
        (home / 'caches/pi.json').unlink(missing_ok=True)
        marker.touch(mode=0o600)


if __name__ == '__main__':
    migrate(Path(sys.argv[1]), sys.argv[2])
