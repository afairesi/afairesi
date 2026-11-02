{ pkgs, ... }:
let
  source = sources.${pkgs.stdenv.hostPlatform.system};
  sources = {
    aarch64-darwin = {
      hash = "b1a530dab7ab28ce359948d7770534d56ac3b0bc39319bc9f9eb4f4931793a75";
      target = "aarch64-apple-darwin";
    };
    aarch64-linux = {
      hash = "0b05019a9a1eab1088c78c31e6133059d3c2bab6c0ed60ce7034047ba8e459fb";
      target = "aarch64-unknown-linux-gnu";
    };
    x86_64-darwin = {
      hash = "651dccb592a0a739c37ef03fa8ab3cf792167a2f4a323b969d6bcc1264df2d05";
      target = "x86_64-apple-darwin";
    };
    x86_64-linux = {
      hash = "206d97279c55b9c0dbc33f5a0bf5a8dfc00a4d828ec4c7e3988e4c7d509cf27d";
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
    url = "https://github.com/Goldziher/${pname}/releases/download/v${version}/${pname}-${source.target}.tar.gz";
  };
  strictDeps = true;
  version = "3.10.4";
}
