{ pkgs, pname, ... }:
pkgs.writeText pname (toString (import ./main.nix).answer)
