{ pkgs, ... }:
let
  source = sources.${pkgs.stdenv.hostPlatform.system};
  sources = {
    aarch64-darwin = {
      hash = "sha256-Vo0UinC5zjAzNDX/wGpkv3nnxSn5E/AMtq+2D3stWWE=";
      target = "aarch64-apple-darwin";
    };
    aarch64-linux = {
      hash = "sha256-y7WzLU8PXsOlpQ/9/HNQAwIxMr4Z9RY8J+rWeawI988=";
      target = "aarch64-unknown-linux-gnu";
    };
    x86_64-darwin = {
      hash = "sha256-mGb0VwG70Z4Jg/xVOO8W/JEV2ARMQCJYYZhS+Mlfc+A=";
      target = "x86_64-apple-darwin";
    };
    x86_64-linux = {
      hash = "sha256-Z+ifzX3TEnPYkQlEoFMuu4sqqhPPcWmEmjnbhiaX6Lk=";
      target = "x86_64-unknown-linux-gnu";
    };
  };
in
pkgs.stdenv.mkDerivation rec {
  buildInputs = pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.stdenv.cc.cc.lib ];
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    test -x "$out/bin/${pname}"
    set -o pipefail
    "$out/bin/${pname}" --help 2>&1 | grep -F "${pname}"
    runHook postInstallCheck
  '';
  installPhase = ''
    runHook preInstall
    install -Dm755 ${pname} "$out/bin/${pname}"
    runHook postInstall
  '';
  meta = {
    description = "Remove comments from source code";
    mainProgram = pname;
    platforms = builtins.attrNames sources;
  };
  nativeBuildInputs = pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.autoPatchelfHook ];
  pname = baseNameOf ./.;
  sourceRoot = ".";
  src = pkgs.fetchurl {
    inherit (source) hash;
    url = "https://github.com/polylint/${pname}/releases/download/v${version}/${pname}-${source.target}.tar.gz";
  };
  strictDeps = true;
  version = "3.12.1";
}
