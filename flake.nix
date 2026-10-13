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
      blueprint = import ./packages/afairesi/prm/blueprint.nix { inherit inputs; };
      flake = blueprint { inherit inputs; };
      lib = import ./packages/afairesi/prm/flake.nix { inherit inputs; };
    in
    flake
    // {
      inherit blueprint lib;
      formatter = lib.mkFormatter { inherit (inputs) self; };
      packages = builtins.mapAttrs (
        _: packages: packages // { default = packages.afairesi; }
      ) flake.packages;
    };
}
