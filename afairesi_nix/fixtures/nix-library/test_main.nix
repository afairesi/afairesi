{
  inputs,
  packageDrv,
  pkgs,
  pname,
  system,
}:
{
  testPackageKeepsItsDeclaredName = {
    expected = "nix-library";
    expr = packageDrv.name;
  };
  testSuiteReceivesTheConsumersPackage = {
    expected = inputs.self.packages.${system}.${pname}.drvPath;
    expr = packageDrv.drvPath;
  };
  testSuiteTargetsTheHostSystem = {
    expected = pkgs.stdenv.hostPlatform.system;
    expr = system;
  };
}
