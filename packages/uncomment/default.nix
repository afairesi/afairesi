{ pkgs, ... }:
pkgs.stdenv.mkDerivation rec {
  buildInputs = [ pkgs.stdenv.cc.cc.lib ];
  doInstallCheck = pkgs.stdenv.hostPlatform.isLinux;
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
  };
  nativeBuildInputs = [ pkgs.autoPatchelfHook ];
  pname = baseNameOf ./.;
  sourceRoot = ".";
  src = pkgs.fetchurl {
    sha256 = "NtMCZkvsoKbeLD5SvF1j09/pamV1TcpTijs7F+Bp2eA=";
    url = "https://github.com/Goldziher/${pname}/releases/download/v${version}/${pname}-x86_64-unknown-linux-gnu.tar.gz";
  };
  strictDeps = true;
  version = "3.9.0";
}
