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
      lib = import ./packages/perigrafo/prm/flake.nix { inherit inputs; };
    in
    lib.mkFlake { inherit inputs; }
    // {
      inherit lib;
      formatter = lib.mkFormatter { inherit (inputs) self; };
    };
}
