{
  inputs = {
    blueprint = {
      inputs.nixpkgs.follows = "nixpkgs";
      url = "github:numtide/blueprint";
    };
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    treefmt-nix = {
      inputs.nixpkgs.follows = "nixpkgs";
      url = "github:numtide/treefmt-nix";
    };
  };
  outputs =
    inputs:
    let
      lib = import ./packages/afairesi/prm/flake.nix { inherit inputs; };
    in
    lib.blueprint { inherit inputs; }
    // {
      inherit lib;
      inherit (lib) blueprint;
      defaultPackage = builtins.mapAttrs (_: packages: packages.afairesi) inputs.self.packages;
      formatter = lib.mkFormatter { inherit (inputs) self; };
    };
}
