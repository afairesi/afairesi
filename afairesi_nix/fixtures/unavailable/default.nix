{ pkgs }:
pkgs.runCommand "unavailable" {
  meta.platforms = [ ];
} "touch $out"
