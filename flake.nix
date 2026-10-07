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
      lib = builtins.removeAttrs shared [ "blueprint" ];
      shared = import ./packages/afairesi/prm/flake.nix { inherit inputs; };
    in
    shared.blueprint { inherit inputs; }
    // {
      inherit lib;
      inherit (shared) blueprint;
      formatter = lib.mkFormatter { inherit (inputs) self; };
    };
}
