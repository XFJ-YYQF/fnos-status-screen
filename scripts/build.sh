#!/usr/bin/env bash
# Build dist/nasscreen-<ver>.fpk and dist/nasscreen-windows-agent-<ver>.zip
# Usage: scripts/build.sh [version]     (default: version from fnos/manifest; a leading "v" is stripped)
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-}"
[ -n "$VERSION" ] || VERSION="$(grep '^version=' fnos/manifest | cut -d= -f2)"
VERSION="${VERSION#v}"
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){1,3}([-.][0-9A-Za-z.]+)?$ ]] || { echo "bad version: $VERSION" >&2; exit 1; }

rm -rf build dist; mkdir -p build/fpk dist
TAR_OPTS=(--owner=0 --group=0 --numeric-owner --sort=name --mtime='2026-01-01 00:00:00')

# ---- fpk (manifest, icons, cmd/, config/, app.tgz)
cp -r fnos/. build/fpk/
sed -i -e '$a\' build/fpk/manifest
sed -i "s/^version=.*/version=$VERSION/; /^checksum=/d" build/fpk/manifest
chmod +x build/fpk/cmd/* build/fpk/app/dashboard.py
tar "${TAR_OPTS[@]}" -czf build/fpk/app.tgz -C build/fpk/app .
echo "checksum=$(md5sum build/fpk/app.tgz | cut -d' ' -f1)" >> build/fpk/manifest
tar "${TAR_OPTS[@]}" -czf "dist/nasscreen-$VERSION.fpk" -C build/fpk manifest ICON.PNG ICON_256.PNG cmd config app.tgz

# ---- Windows agent (batch files must be CRLF)
mkdir -p build/win
cp windows-agent/* build/win/
for f in build/win/*.bat build/win/README.txt; do sed -i 's/\r$//; s/$/\r/' "$f"; done
(cd build/win && zip -q -X "../../dist/nasscreen-windows-agent-$VERSION.zip" nasscreen_agent.py install.bat uninstall.bat README.txt)

# ---- checksums + release notes
(cd dist && sha256sum * > SHA256SUMS.txt)
REPO="${GITHUB_REPOSITORY:-OWNER/REPO}"
cat > build/release-notes.md <<NOTES
## 安装

- **NAS 端**:下载 \`nasscreen-$VERSION.fpk\` → 飞牛 fnOS 应用中心 → 手动安装 → 启动应用
- **Windows 端(可选)**:下载 \`nasscreen-windows-agent-$VERSION.zip\`,解压后右键 \`install.bat\` 以管理员身份运行

详细说明见 [README](https://github.com/$REPO#readme)。

## 校验(SHA-256)

\`\`\`
$(cat dist/SHA256SUMS.txt)
\`\`\`
NOTES
echo "built $VERSION:"; ls -l dist
