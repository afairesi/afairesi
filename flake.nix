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
      lib = (import ./afairesi/prm/builders.nix) // {
        mkFlake = import ./afairesi/prm/mk-flake.nix inputs;
      };
    in
    flake
    // {
      inherit lib;
      packages = builtins.mapAttrs (
        _: packages: packages // { default = packages.afairesi; }
      ) flake.packages;
    };
}
