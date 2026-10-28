{
  check,
  packageDrv,
}:
let
  # VM wrappers retain derivation attributes but do not expose overrideAttrs.
  overrideCheck =
    check.overrideAttrs or (update: builtins.derivation (check.drvAttrs // update check));
  # Extract standalone helpers without importing the CLI or its dependencies.
  runtime = builtins.toFile "coverage_support.py" (
    "from __future__ import annotations\n"
    + builtins.elemAt (builtins.split "COVERAGE_RUNTIME_(START|END) = None\n" (builtins.readFile ../main.py)) 2
  );
  context = builtins.toFile "coverage_context.py" (
    "from _afairesi_coverage_support import _coverage_context_plugin\n"
    + "pytest_runtest_protocol = _coverage_context_plugin()\n"
  );
  startup = builtins.toFile "sitecustomize.py" ("import coverage\n" + "coverage.process_startup()\n");
in
overrideCheck (previous: {
  name = "${previous.name}-coverage";
  buildCommand = ''
    mkdir -p "$out/html" "$TMPDIR/coverage-startup"
    export COVERAGE_PROCESS_START="$TMPDIR/coverage.ini"
    afairesi_pytest_args=(-p _afairesi_coverage_context)
    unset AFAIRESI_TEST_CONTEXT
    cp "${context}" "$TMPDIR/coverage-startup/_afairesi_coverage_context.py"
    cp "${startup}" "$TMPDIR/coverage-startup/sitecustomize.py"
    cp "${runtime}" "$TMPDIR/coverage-startup/_afairesi_coverage_support.py"
    cat > "$COVERAGE_PROCESS_START" <<EOF
    [run]
    core = ctrace
    context = \$AFAIRESI_TEST_CONTEXT
    parallel = true
    branch = true
    data_file = $out/.coverage
    source =
        $src
        ${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}
    omit =
        */test_main.py
        */prm/*
    EOF
    export PYTHONPATH="$TMPDIR/coverage-startup:$PWD:${packageDrv.python.pkgs.coverage}/${packageDrv.python.sitePackages}:$PYTHONPATH"
  ''
  + previous.buildCommand
  + ''
    unset COVERAGE_PROCESS_START
    python -m coverage combine --rcfile="$TMPDIR/coverage.ini"
    python -c 'from _afairesi_coverage_support import remap_coverage_paths; remap_coverage_paths("${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}/__init__.py")'
    python -m coverage html --show-contexts --rcfile="$TMPDIR/coverage.ini" -d "$out/html"
    python -m coverage json --show-contexts --rcfile="$TMPDIR/coverage.ini" -o "$out/coverage.json"
  '';
})
