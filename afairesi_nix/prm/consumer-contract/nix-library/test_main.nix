{ inputs, pkgs, pname, system, packageDrv }:
assert pname == "nix-library";
assert system == pkgs.stdenv.hostPlatform.system;
assert packageDrv == inputs.self.packages.${system}.${pname};
assert (import ./main.nix).answer == 42;
pkgs.runCommand "${pname}-test" { } ''
  test "$(cat ${packageDrv})" = 42
  touch "$out"
''
