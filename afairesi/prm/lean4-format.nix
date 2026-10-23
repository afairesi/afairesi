{
  pkgs,
  packageDrv,
  packageName,
}:
let
  testEnvironment = (import ./builders.nix).mkTestEnvironment { inherit pkgs packageDrv; };
  leanPath = pkgs.lib.makeSearchPath ".lake/build/lib/lean" (
    pkgs.lib.concatMap (
      dependency: [ dependency ] ++ (dependency.allLeanDeps or [ ])
    ) testEnvironment.dependencies
  );
in
pkgs.writeShellApplication {
  name = "lean4-format-${packageName}";
  runtimeInputs = testEnvironment.dependencies ++ [
    (import ./lean-fmt.nix { inherit pkgs; })
    packageDrv.lean4
    pkgs.coreutils
    pkgs.diffutils
  ];
  text = ''
    source_root="$PWD/${packageName}"
    workspace=$(mktemp -d)
    trap 'rm -rf "$workspace"' EXIT
    cp -R ${packageDrv}/. "$workspace"
    chmod -R u+w "$workspace"
    printf '%s\n' 'leanprover/lean4:v${packageDrv.lean4.version}' > "$workspace/lean-toolchain"
    files=()
    declare -A original_hashes=()
    for source in "$@"; do
      relative=$(realpath --relative-to="$source_root" "$source")
      cp "$source" "$workspace/$relative"
      original_hashes["$relative"]=$(sha256sum "$workspace/$relative" | cut -d ' ' -f1)
      files+=("$relative")
    done
    export LEAN_PATH=${pkgs.lib.escapeShellArg leanPath}
    export LEAN_SYSROOT=${packageDrv.lean4}
    export LEAN_CC=${pkgs.stdenv.cc}/bin/cc
    export LAKE_NO_CACHE=1
    ${pkgs.lib.optionalString (packageDrv.meta ? mainProgram) ''
      export PACKAGE_E2E_EXECUTABLE=${pkgs.lib.getExe packageDrv}
    ''}
    format_status=0
    lean-fmt format --root "$workspace" --no-cache "''${files[@]}" || format_status=$?
    for relative in "''${files[@]}"; do
      if ! cmp -s "$workspace/$relative" "$source_root/$relative"; then
        current_hash=$(sha256sum "$source_root/$relative" | cut -d ' ' -f1) || current_hash=""
        if [ "$current_hash" != "''${original_hashes[$relative]}" ]; then
          printf 'File changed while formatting; rerun formatter: %s\n' "$source_root/$relative" >&2
          format_status=1
          continue
        fi
        cp "$workspace/$relative" "$source_root/$relative"
      fi
    done
    if [ "$format_status" -ne 0 ]; then
      exit "$format_status"
    fi
    lean-fmt check --root "$workspace" --no-cache --select all "''${files[@]}"
  '';
}
