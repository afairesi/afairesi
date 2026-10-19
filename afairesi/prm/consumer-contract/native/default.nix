{
  inputs,
  pkgs,
  pname,
  system,
}:
pkgs.runCommand pname {
  passthru = {
    example = inputs.self.packages.${system}.example;
    tests.contract = pkgs.runCommand "native-contract" { } "touch $out";
  };
} "touch $out"
