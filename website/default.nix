{ pkgs, ... }:
let
  pname = baseNameOf ./.;
  site = pkgs.runCommand "${pname}-site" { } ''
    mkdir -p "$out"
    cp ${./index.html} "$out/index.html"
    for asset in script.js style.css; do
      if [ -f ${./.}/"$asset" ]; then
        cp ${./.}/"$asset" "$out/$asset"
      fi
    done
    if [ -d ${./.}/prm ]; then
      cp -R ${./.}/prm "$out/prm"
      chmod -R u+w "$out/prm"
    fi
    runHook postInstall
  '';
in
pkgs.writeShellApplication {
  meta.description = "A CLI and Nix flake library for organizing your home directory and Git repositories.";
  name = pname;
  runtimeInputs = [
    pkgs.http-server
  ]
  ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.xdg-utils ];
  text = ''
    open_args=()
    if [[ -n "''${DISPLAY:-}" || -n "''${WAYLAND_DISPLAY:-}" ]]; then
      open_args=(-o /)
    fi
    server_args=()
    for argument in "$@"; do
      case "$argument" in
        --no-open)
          open_args=()
          ;;
        -o|--o|-o=*|--o=*|--no-o)
          open_args=()
          server_args+=("$argument")
          ;;
        -h|--help)
          printf '%s\n' 'Desktop runs open the browser; --no-open disables this.'
          server_args+=("$argument")
          ;;
        *)
          server_args+=("$argument")
          ;;
      esac
    done
    exec http-server ${site} "''${open_args[@]}" "''${server_args[@]}"
  '';
}
