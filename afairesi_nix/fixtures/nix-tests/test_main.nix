{ packageDrv, pkgs, ... }:
assert !(builtins.pathExists ./main.nix);
pkgs.runCommand "nix-tests-check" { } ''
  test "$(cat ${packageDrv})" = 'tested without main.nix'
  touch "$out"
''
