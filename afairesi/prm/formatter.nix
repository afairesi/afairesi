{
  enableMypy ? true,
  inputs,
  pkgs,
  self ? inputs.self,
}:
let
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
  workflowFiles = [
    ".github/workflows/workflow.yml"
    ".forgejo/workflows/workflow.yml"
  ];
  formatterPython = pkgs.python3.withPackages (_: [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
  ]);
  pythonPackageNames =
    if enableMypy then
      builtins.attrNames (
        pkgs.lib.filterAttrs (
          name: type:
          type == "directory"
          && !(builtins.elem name [
            "prm"
            "tmp"
          ])
          && builtins.pathExists (self.outPath + "/${name}/main.py")
        ) (if builtins.pathExists self.outPath then builtins.readDir self.outPath else { })
      )
    else
      [ ];
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
        nix-alphabetize = {
          command = inputs.self.packages.${pkgs.stdenv.system}.nix_alphabetize;
          includes = nixFiles;
          priority = 4;
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
      });
      global.excludes = [ "{prm,tmp,*/prm,*/tmp}/**" ];
    };
  };
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
    tests.check = treefmtEval.config.build.check self.outPath;
  };
}
