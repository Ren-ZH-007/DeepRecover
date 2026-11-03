#!/usr/bin/env bash
# Native macOS only. This produces an ad-hoc signed, non-notarized preview .app.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'A native macOS runner is required; cross-compilation is not supported.' >&2
  exit 1
fi
build_arch="${1:-$(uname -m)}"
case "$build_arch" in arm64|x86_64) ;; *) echo 'Unsupported architecture' >&2; exit 1 ;; esac
if [[ "$(uname -m)" != "$build_arch" ]]; then
  echo 'Runner architecture does not match the requested native build.' >&2
  exit 1
fi
build_seed_python="${DEEPRECOVER_BUILD_PYTHON:-python3}"
"$build_seed_python" -m venv .venv
build_python="$PWD/.venv/bin/python"
"$build_python" - "$build_arch" <<'PY'
import platform,struct,sys
assert platform.system()=='Darwin' and platform.machine()==sys.argv[1]
assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8
PY
build_version="$("$build_python" -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
if [[ ! "$build_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then echo 'Invalid project version' >&2; exit 1; fi
if [[ "${GITHUB_REF_TYPE:-}" == tag && "${GITHUB_REF_NAME:-}" != "v$build_version" ]]; then
  echo 'Tag and project version differ.' >&2
  exit 1
fi
export MACOSX_DEPLOYMENT_TARGET=13.0
export QT_QPA_PLATFORM=offscreen
build_dir="$PWD/build/macos-$build_arch"
build_dist="$PWD/dist/macos-$build_arch"
mkdir -p "$build_dir" "$build_dist"
"$build_python" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller==6.12.0
"$build_python" -m pip check
"$build_python" scripts/run_release_tests.py --report "$build_dir/test-report.json"
"$build_python" - "$build_dir/test-report.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1],encoding='utf-8'))
assert data['success'] is True and data['run']>=103 and not data['skipped']
PY
"$build_python" scripts/create_app_icon.py --output "$build_dir/DeepRecover.iconset"
/usr/bin/iconutil -c icns -o "$build_dir/DeepRecover.icns" "$build_dir/DeepRecover.iconset"
"$build_python" -m PyInstaller --noconfirm --clean --windowed --onedir \
  --name DeepRecover --target-architecture "$build_arch" \
  --osx-bundle-identifier com.deeprecover.workbench --icon "$build_dir/DeepRecover.icns" \
  --collect-data app --specpath "$build_dir/spec" --workpath "$build_dir/pyinstaller" \
  --distpath "$build_dist" main.py
build_bundle="$build_dist/DeepRecover.app"
"$build_python" - "$build_bundle" "$build_version" <<'PY'
from pathlib import Path
import json,plistlib,sys
bundle=Path(sys.argv[1]); path=bundle/'Contents/Info.plist'
with path.open('rb') as stream: data=plistlib.load(stream)
data.update(CFBundleIdentifier='com.deeprecover.workbench',CFBundleDisplayName='DeepRecover',
            CFBundleShortVersionString=sys.argv[2],CFBundleVersion=sys.argv[2],
            LSMinimumSystemVersion='13.0',LSApplicationCategoryType='public.app-category.utilities',
            NSHighResolutionCapable=True)
with path.open('wb') as stream: plistlib.dump(data,stream)
PY
# Changing Info.plist invalidates the enclosing signature. Local ad-hoc signing
# is explicit and never described as Developer ID signing or Apple notarization.
/usr/bin/codesign --force --deep --sign - --timestamp=none "$build_bundle"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$build_bundle" 2>&1 | tee "$build_dir/codesign-validation.log"
/usr/bin/codesign --display --verbose=4 "$build_bundle" > "$build_dir/codesign-details.log" 2>&1
rm -f "$build_dir/offscreen-smoke.json" "$build_dir/cocoa-smoke.json" "$build_dir/cocoa-overview.png"
"$build_python" scripts/verify_macos_build.py --bundle "$build_bundle" \
  --version "$build_version" --arch "$build_arch" --qt-platform offscreen --report "$build_dir/offscreen-smoke.json"
build_archive="$build_dist/DeepRecover-v$build_version-macos-$build_arch.zip"
rm -f "$build_archive"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$build_bundle" "$build_archive"
(cd "$build_dist" && shasum -a 256 "$(basename "$build_archive")" > SHA256SUMS.txt)
build_distribution_check="$build_dir/distribution-check"
rm -rf -- "$build_distribution_check"
mkdir -p "$build_distribution_check"
/usr/bin/ditto -x -k "$build_archive" "$build_distribution_check"
build_extracted_bundle="$build_distribution_check/DeepRecover.app"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$build_extracted_bundle" 2>&1 | tee "$build_dir/codesign-distribution.log"
"$build_python" scripts/verify_macos_build.py --bundle "$build_extracted_bundle" \
  --version "$build_version" --arch "$build_arch" --qt-platform cocoa --report "$build_dir/cocoa-smoke.json" \
  --screenshot "$build_dir/cocoa-overview.png"
"$build_python" - "$build_extracted_bundle" "$build_dir/bundle-validation.json" "$build_arch" "$build_version" <<'PY'
from pathlib import Path
import json,platform,plistlib,subprocess,sys
bundle=Path(sys.argv[1])
with (bundle/'Contents/Info.plist').open('rb') as stream: info=plistlib.load(stream)
payload={'success':True,'architecture':sys.argv[3],'version':sys.argv[4],
         'runner_macos':platform.mac_ver()[0],'python':platform.python_version(),
         'minimum_macos':info['LSMinimumSystemVersion'],'bundle_identifier':info['CFBundleIdentifier'],
         'mach_o_archs':subprocess.check_output(['/usr/bin/lipo','-archs',str(bundle/'Contents/MacOS/DeepRecover')],text=True).strip(),
         'signing':'local ad-hoc; no Developer ID certificate; not notarized',
         'offscreen_smoke':True,'cocoa_smoke':True,'zip_roundtrip_verified':True,'physical_device_acceptance':False}
Path(sys.argv[2]).write_text(json.dumps(payload,indent=2),encoding='utf-8')
PY
echo "PASS: $build_archive; native $build_arch, offscreen+Cocoa verified; ad-hoc signed, not notarized."
