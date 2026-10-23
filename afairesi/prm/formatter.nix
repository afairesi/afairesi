{
  enableMypy ? true,
  inputs,
  pkgs,
  self ? inputs.self,
}:
let
  formatterPython = pkgs.python3.withPackages (_: [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
  ]);
  nixFiles = [
    "flake.nix"
    "*/default.nix"
    "*/configuration.nix"
    "*/hardware-configuration.nix"
  ];
  pythonFiles = [
    "*/main.py"
    "*/test_main.py"
  ];
  packageNamesFor =
    marker:
    builtins.attrNames (
      pkgs.lib.filterAttrs (
        name: _:
        builtins.pathExists (self.outPath + "/${name}/default.nix")
        && builtins.pathExists (self.outPath + "/${name}/${marker}")
      ) self.packages.${pkgs.stdenv.system}
    );
  lean4PackageNames = packageNamesFor "main.lean";
  pythonPackageNames = if enableMypy then packageNamesFor "main.py" else [ ];
  rawFormatter = treefmtEval.config.build.wrapper;
  treefmtEval = inputs.treefmt-nix.lib.evalModule pkgs {
    programs = {
      actionlint = {
        enable = true;
        includes = workflowFiles;
        priority = 6;
      };
      deadnix = {
        enable = true;
        includes = nixFiles;
        priority = 3;
      };
      mypy = {
        directories = pkgs.lib.genAttrs pythonPackageNames (
          name:
          let
            environment = inputs.self.lib.mkTestEnvironment {
              inherit pkgs;
              packageDrv = self.packages.${pkgs.stdenv.system}.${name};
            };
          in
          {
            directory = "";
            modules = [ "${name}" ];
            options = [
              "--python-executable=${environment.python}/bin/python"
              "--cache-dir=${name}/tmp/.mypy_cache"
              "--exclude=/(prm|tmp)/"
              "--no-namespace-packages"
              "--ignore-missing-imports"
              "--strict"
            ];
          }
        );
        enable = enableMypy;
      };
      nixfmt = {
        enable = true;
        includes = nixFiles;
        priority = 5;
      };
      oxfmt = {
        enable = true;
        includes = workflowFiles ++ [
          "*/style.css"
          "*/index.html"
          "*/script.js"
        ];
        priority = 5;
      };
      ruff-check = {
        enable = true;
        extendSelect = [ "ALL" ];
        includes = pythonFiles;
        priority = 3;
      };
      ruff-format = {
        enable = true;
        includes = pythonFiles;
        priority = 5;
      };
      statix = {
        enable = true;
        includes = nixFiles;
        priority = 3;
      };
      texfmt = {
        enable = true;
        includes = [ "*/ms.tex" ];
        priority = 5;
      };
      yamllint = {
        enable = true;
        includes = workflowFiles;
        priority = 6;
      };
    };
    projectRootFile = "flake.nix";
    settings = {
      formatter = {
        alphabetize-nix = {
          command = inputs.self.packages.${pkgs.stdenv.system}.alphabetize_nix;
          includes = nixFiles;
          priority = 4;
        };
        bibtex-tidy = {
          command = pkgs.bibtex-tidy;
          includes = [ "*/ms.bib" ];
          options = [
            "--duplicates"
            "--no-align"
            "--no-wrap"
            "--sort"
            "--sort-fields"
            "--v2"
          ];
          priority = 5;
        };
        oxlint = {
          command = pkgs.oxlint;
          includes = [ "*/script.js" ];
          options = [
            "--fix-dangerously"
            "-D"
            "all"
          ];
          priority = 3;
        };
        remove-empty-lines = {
          command = inputs.self.packages.${pkgs.stdenv.system}.remove_empty_lines;
          includes = [
            ".gitignore"
            "LICENSE"
            "flake.lock"
          ];
          priority = 2;
        };
        ruff-check.options = [
          "--cache-dir=tmp/.ruff_cache"
          "--per-file-ignores=test_main.py:INP001"
          "--unsafe-fixes"
        ];
        ruff-format.options = [ "--cache-dir=tmp/.ruff_cache" ];
        uncomment = {
          command = inputs.self.packages.${pkgs.stdenv.system}.uncomment;
          includes = [
            ".gitignore"
            "LICENSE"
            "flake.lock"
          ]
          ++ nixFiles
          ++ pythonFiles
          ++ [
            "*/index.html"
            "*/script.js"
            "*/style.css"
            "*/ms.tex"
            "*/ms.bib"
          ]
          ++ workflowFiles;
          priority = 1;
        };
      }
      // pkgs.lib.genAttrs (map (name: "mypy-${name}") pythonPackageNames) (_: {
        priority = 6;
      })
      // pkgs.lib.genAttrs' lean4PackageNames (name: {
        name = "lean4-${name}";
        value = {
          command = import ./lean4-format.nix {
            inherit pkgs;
            packageDrv = self.packages.${pkgs.stdenv.system}.${name};
            packageName = name;
          };
          includes = [ "${name}/*.lean" ];
          priority = 5;
        };
      });
      global.excludes = [ "{prm,tmp,*/prm,*/tmp}/**" ];
    };
  };
  workflowFiles = [
    ".github/workflows/workflow.yml"
    ".forgejo/workflows/workflow.yml"
  ];
  wrapper = pkgs.writeShellApplication {
    name = "treefmt";
    runtimeInputs = [
      pkgs.git
      pkgs.nix
    ];
    text = ''
      ${formatterPython}/bin/python -c 'from afairesi import formatter_main; formatter_main()'
      exec ${rawFormatter}/bin/treefmt "$@"
    '';
  };
in
wrapper
// {
  passthru = wrapper.passthru // {
    raw = rawFormatter;
    tests.check = (treefmtEval.config.build.check self.outPath).overrideAttrs (previous: {
      nativeBuildInputs = (previous.nativeBuildInputs or [ ]) ++ [
        pkgs.git
        pkgs.nix
      ];
      buildCommand = ''
        ${formatterPython}/bin/python -c 'from afairesi import formatter_main; formatter_main()' --source ${self.outPath}
      ''
      + previous.buildCommand;
    });
  };
}
