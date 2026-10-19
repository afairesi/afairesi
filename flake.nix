{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    treefmt-nix = {
      inputs.nixpkgs.follows = "nixpkgs";
      url = "github:numtide/treefmt-nix";
    };
  };
  outputs =
    inputs:
    let
      flake = lib.mkFlake { inherit inputs; };
      lib = import ./packages/afairesi/prm/flake.nix { inherit inputs; };
    in
    flake
    // {
      inherit lib;
      formatter = lib.mkFormatter { inherit (inputs) self; };
      packages = builtins.mapAttrs (
        _: packages: packages // { default = packages.afairesi; }
      ) flake.packages;
    };
}
