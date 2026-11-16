{ inputs, pkgs, ... }:
let
  python = pkgs.python3;
in
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "afairesi CLI for inspecting repositories in the afairesi computing environment.";
  passthru = {
    checkEnv.AFAIRESI_NIX_SOURCE = ../afairesi_nix;
    checkInputs = [
      python.pkgs.coverage
      ((import ../afairesi_nix/main.nix inputs).formatter {
        inherit inputs pkgs;
      })
    ];
  };
  postFixup = ''
    touch "$out/${python.sitePackages}/$pname/py.typed"
  '';
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.nix_syntax
    pkgs.git
    pkgs.jd-diff-patch
    pkgs.nix
    pkgs.python3.pkgs.libcst
  ];
  src = ./.;
}
