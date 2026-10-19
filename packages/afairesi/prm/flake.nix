{ inputs }:
let
  builders = import ./builders.nix;
  lib = builders // {
    mkFlake = import ./mk-flake.nix { inherit inputs; };
    mkFormatter =
      { self }:
      inputs.nixpkgs.lib.genAttrs (builtins.attrNames self.packages) (
        system:
        import ../../../formatter.nix {
          inherit inputs self;
          flake = self.outPath;
          pkgs = inputs.nixpkgs.legacyPackages.${system};
        }
      );
  };
in
lib
