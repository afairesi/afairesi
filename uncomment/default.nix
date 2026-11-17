{ pkgs, ... }:
let
  source = sources.${pkgs.stdenv.hostPlatform.system};
  sources = {
    aarch64-darwin = {
      hash = "568d148a70b9ce30333435ffc06a64bf79e7c529f913f00cb6afb60f7b2d5961";
      target = "aarch64-apple-darwin";
    };
    aarch64-linux = {
      hash = "cbb5b32d4f0f5ec3a5a50ffdfc735003023132be19f5163c27ead679ac08f7cf";
      target = "aarch64-unknown-linux-gnu";
    };
    x86_64-darwin = {
      hash = "9866f45701bbd19e0983fc5538ef16fc9115d8044c402258619852f8c95f73e0";
      target = "x86_64-apple-darwin";
    };
    x86_64-linux = {
      hash = "67e89fcd7dd31273d8910944a0532ebb8b2aaa13cf7169849a39db862697e8b9";
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
    sha256 = source.hash;
    url = "https://github.com/polylint/${pname}/releases/download/v${version}/${pname}-${source.target}.tar.gz";
  };
  strictDeps = true;
  version = "3.12.1";
}
