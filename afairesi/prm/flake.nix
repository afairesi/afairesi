{ inputs }:
(import ./builders.nix)
// {
  mkFlake = import ./mk-flake.nix { inherit inputs; };
}
