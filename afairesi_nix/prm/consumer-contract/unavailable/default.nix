{ pkgs }:
pkgs.runCommand "unavailable" {
  meta.platforms = [ "unavailable-system" ];
} "touch $out"
