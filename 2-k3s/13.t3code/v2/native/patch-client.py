#!/usr/bin/env python3
"""Disable Linux system URI registration only in our separately packaged v2 build."""
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
changes = {
    'apps/desktop/src/app/DesktopPreReadyPlatform.ts': (
        '        NodeFS.writeFileSync(',
        '        if (process.env.T3CODE_V2_NO_URI_REGISTRATION !== "1") NodeFS.writeFileSync(',
    ),
    'apps/desktop/src/app/DesktopLinuxUrlHandler.ts': (
        '    if (environment.platform !== "linux") {',
        '    if (environment.platform !== "linux" || process.env.T3CODE_V2_NO_URI_REGISTRATION === "1") {',
    ),
    'apps/desktop/src/app/DesktopClerk.ts': (
        '  return createClerkBridge({',
        '  return createClerkBridge({',
    ),
}
for relative, (old, new) in changes.items():
    path = root / relative
    content = path.read_text()
    if content.count(old) != 1:
        raise SystemExit('Upstream URI registration changed; packaging patch requires review: ' + relative)
    if relative.endswith('DesktopClerk.ts'):
        anchor = 'function createDesktopClerkBridge(stateDir: string, isDevelopment: boolean) {'
        if content.count(anchor) != 1:
            raise SystemExit('Upstream Clerk bridge changed; packaging patch requires review')
        content = content.replace(anchor, anchor + '\n  if (process.env.T3CODE_V2_NO_URI_REGISTRATION === "1") {\n    Electron.app.setAsDefaultProtocolClient = () => false;\n  }')
        content = content.replace('import { createClerkBridge } from "@clerk/electron";', 'import { createClerkBridge } from "@clerk/electron";\nimport * as Electron from "electron";')
        path.write_text(content)
    else:
        path.write_text(content.replace(old, new))
