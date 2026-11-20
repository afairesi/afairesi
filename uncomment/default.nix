{ pkgs, ... }:
let
  inherit (pkgs.stdenv) hostPlatform;
  hashes = {
    aarch64-darwin = "sha256-Vo0UinC5zjAzNDX/wGpkv3nnxSn5E/AMtq+2D3stWWE=";
    aarch64-linux = "sha256-y7WzLU8PXsOlpQ/9/HNQAwIxMr4Z9RY8J+rWeawI988=";
    x86_64-linux = "sha256-Z+ifzX3TEnPYkQlEoFMuu4sqqhPPcWmEmjnbhiaX6Lk=";
  };
in
pkgs.stdenv.mkDerivation rec {
  buildInputs = pkgs.lib.optionals hostPlatform.isLinux [ pkgs.stdenv.cc.cc.lib ];
  doInstallCheck = true;
  installPhase = ''
    runHook preInstall
    install -Dm755 "$pname" "$out/bin/$pname"
    runHook postInstall
  '';
  meta = {
    description = "Remove comments from source code";
    mainProgram = pname;
    platforms = builtins.attrNames hashes;
  };
  nativeBuildInputs = pkgs.lib.optionals hostPlatform.isLinux [ pkgs.autoPatchelfHook ];
  nativeInstallCheckInputs = [ pkgs.versionCheckHook ];
  pname = baseNameOf ./.;
  sourceRoot = ".";
  src = pkgs.fetchurl {
    hash = hashes.${hostPlatform.system};
    url = "https://github.com/polylint/${pname}/releases/download/v${version}/${pname}-${hostPlatform.rust.rustcTarget}.tar.gz";
  };
  strictDeps = true;
  version = "3.12.1";
}
