{ inputs, pkgs }:
let
  inherit (import ./test_fixtures.nix { inherit inputs pkgs; })
    consumer
    consumerSource
    htmlPackage
    overridden
    overriddenPackage
    package
    pythonLibrary
    system
    ;
  accepts =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkPythonPackage (
        {
          inherit pkgs;
          executable = true;
          src = ./fixtures/example;
        }
        // attributes
      )
    )).success;
  acceptsLean =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkLean4Package (
        {
          inherit pkgs;
          executable = true;
          src = ./fixtures/lean-example;
        }
        // attributes
      )
    )).success;
  apps = consumer.apps.${system};
  check = expected: expr: { inherit expected expr; };
  checks = consumer.checks.${system};
  empty = inputs.self.lib.mkFlake {
    inputs.self = empty // {
      outPath = consumerSource + "/unavailable";
    };
    systems = [ system ];
  };
  leanExample = consumer.packages.${system}.lean-example;
  leanLibrary = consumer.packages.${system}.test_main;
  overriddenCheck = overridden.checks.${system}.example;
  overriddenEnvironment = inputs.self.lib.mkTestEnvironment {
    inherit pkgs;
    packageDrv = overriddenPackage;
  };
  packages = consumer.packages.${system};
  shells = consumer.devShells.${system};
in
{
  testAfairesiIsTheDefaultPackageAndNeedsNoAppWrapper = check true (
    !(inputs.self.apps.${system} ? afairesi)
    && inputs.self.packages.${system}.default == inputs.self.packages.${system}.afairesi
  );
  testAppsExposePackageMetadata = check true (
    apps.example.type == "app"
    && apps.example.meta == package.meta
    && apps.example.meta.description == "Afairesi consumer contract example"
  );
  testAppsSupplyADescriptionWhenThePackageHasNone =
    check "Run example"
      overridden.apps.${system}.example.meta.description;
  testBuildersDeclareTheirPackageKind =
    check
      [ "python" "bash" "html" "lean4" ]
      [
        package.afairesiKind
        packages.bash-example.afairesiKind
        htmlPackage.afairesiKind
        leanExample.afairesiKind
      ];
  testChecksIncludeSupportedPackageTests = check true (
    checks.pkgs-python_native-contract == packages.python_native.passthru.tests.contract
    && !(overridden.checks.${system} ? pkgs-example-unavailable)
  );
  testChecksIncludeTheFormatterAndAllPackages = check true (
    checks.formatter == consumer.formatter.${system}.passthru.tests.check
    && pkgs.lib.all (name: checks."pkgs-${name}" == packages.${name}) (builtins.attrNames packages)
  );
  testEachTestShellHasAMatchingCheck = check true (
    pkgs.lib.all (name: checks."devshell-${name}" == shells.${name}) (builtins.attrNames shells)
  );
  testEmptyRepositoriesExposeOnlyRepositoryChecksAndAFormatter =
    check
      {
        apps = { };
        checks = [
          "formatter"
          "lint"
          "structure"
        ];
        formatter = [ system ];
        hosts = { };
        packages = { };
        shells = { };
      }
      {
        apps = empty.apps.${system};
        checks = builtins.attrNames empty.checks.${system};
        formatter = builtins.attrNames empty.formatter;
        hosts = empty.nixosConfigurations;
        packages = empty.packages.${system};
        shells = empty.devShells.${system};
      };
  testEveryPackageSystemHasAFormatter = check (builtins.attrNames consumer.packages) (
    builtins.attrNames consumer.formatter
  );
  testExecutableChecksUseTheOverriddenMainProgram = check "${overriddenPackage}/bin/renamed" overriddenCheck.PACKAGE_E2E_EXECUTABLE;
  testExecutablesExposeTheirPackageName =
    check
      [ "example" "lean-example" ]
      [
        package.meta.mainProgram
        leanExample.meta.mainProgram
      ];
  testHostAndVmChecksUseTheHostPlatform =
    check
      [
        (system == "x86_64-linux")
        (system == "x86_64-linux")
      ]
      [
        (checks ? nixos-example)
        (checks ? exampleVm && builtins.isString checks.exampleVm.drvPath)
      ];
  testHostsReceiveConsumerConfigurationAndOverlays =
    let
      host = consumer.nixosConfigurations.example;
    in
    check
      [ true true true "example" ]
      [
        host.pkgs.config.allowUnfree
        host.pkgs.config.cudaSupport
        host.pkgs.afairesiContract
        host.config.networking.hostName
      ];
  testHtmlPackagesKeepTheirDescription = check "Source listing" htmlPackage.meta.description;
  testLibrariesHaveNoMainProgram =
    check
      [ false false ]
      [
        (pythonLibrary.meta ? mainProgram)
        (leanLibrary.meta ? mainProgram)
      ];
  testLibrariesHaveNoRunnableApp = check false (apps ? test_main);
  testLibraryBuildersRejectRuntimeHooks =
    check
      [ false false ]
      [
        (accepts {
          executable = false;
          runtimeHook = "echo invalid";
        })
        (acceptsLean {
          executable = false;
          runtimeHook = "echo invalid";
        })
      ];
  testLibraryChecksOmitTheExecutablePath = check false (
    inputs.self.checks.${system}.nix_syntax ? PACKAGE_E2E_EXECUTABLE
  );
  testNativeBuildersKeepTheirOwnPackageAttributes =
    check
      [ false false true ]
      [
        (packages.python_native ? afairesiKind)
        (packages.rust-native ? afairesiKind)
        (packages.python_native.example == package)
      ];
  testPackageBuildersReceiveConsumerConfigurationAndOverlays = check {
    afairesiContract = true;
    allowUnfree = true;
    cudaSupport = true;
  } package.consumerConfig;
  testPackagesWithTestEnvironmentsExposeShells = check [
    "bash-example-test"
    "example-test"
    "lean-example-test"
    "test_main-test"
  ] (builtins.attrNames shells);
  testPythonBuilderRequiresImportableModuleNames = check false (
    accepts { src = "/invalid/123"; } || accepts { src = "/invalid/class"; }
  );
  testPythonBuilderReservesThePackageInitializer = check false (accepts {
    src = ./fixtures/python_native;
  });
  testPythonCheckDependenciesAreAvailableToChecksAndShells = check true (
    pkgs.lib.all
      (
        input:
        builtins.elem input overriddenCheck.nativeBuildInputs
        && builtins.elem input overriddenEnvironment.python.paths
      )
      [
        pkgs.python3Packages.six
        pkgs.python3Packages.colorama
      ]
  );
  testPythonPackageNamesAreImportable = check "lean_example" pythonLibrary.pname;
  testRuntimeHooksStayOutsideThePackageBuild =
    check
      [ false false ]
      [
        (package.drvAttrs ? runtimeHook)
        (leanExample.drvAttrs ? runtimeHook)
      ];
  testSuiteChecksRequireAPackageWithAMatchingSuite =
    check
      [ true false false false ]
      [
        (checks ? example)
        (checks ? orphan)
        (checks ? nix-untested)
        (checks ? python_native)
      ];
  testUnavailablePackagesAreOmitted =
    check
      [ false false ]
      [
        (packages ? unavailable)
        (checks ? unavailable)
      ];
}
