{ inputs, pkgs, ... }:
let
  python = pkgs.python3;
in
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Manage persistent home and flake repository structure";
  passthru = {
    checkInputs = [
      (import ../../formatter.nix {
        inherit inputs pkgs;
        enableMypy = false;
        flake = inputs.self.outPath;
      }).passthru.raw
    ];
    tests.consumer-contract = import ./prm/consumer-contract.nix { inherit inputs pkgs; };
  };
  postFixup = ''
    touch "$out/${python.sitePackages}/$pname/py.typed"
  '';
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.nix_syntax
    pkgs.git
    pkgs.nix
    pkgs.python3.pkgs.libcst
  ];
  src = ./.;
  version = "0.0.0";
}
