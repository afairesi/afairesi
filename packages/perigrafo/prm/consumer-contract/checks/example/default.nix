{ pkgs, ... }:
pkgs.runCommand "example-check" { } "mkdir -p $out"
