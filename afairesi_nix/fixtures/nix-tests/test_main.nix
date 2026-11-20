{
  testSuiteRunsWithoutAMainFile = {
    expected = false;
    expr = builtins.pathExists ./main.nix;
  };
}
