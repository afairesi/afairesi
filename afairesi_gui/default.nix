{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Inspect home submodules, resource interfaces, and differences locally or over SSH.";
  passthru.checkInputs = [
    pkgs.nix
    pkgs.nodejs
  ];
  postFixup = ''
    touch "$out/${pkgs.python3.sitePackages}/$pname/py.typed"
  '';
  postPatch = ''
    substituteInPlace main.py \
      --replace-fail 'SOURCE_REVISION = ""' 'SOURCE_REVISION = "${inputs.self.rev or ""}"'
  '';
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
    pkgs.git
    pkgs.openssh
  ];
  src = ./.;
}
