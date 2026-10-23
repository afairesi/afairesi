import «lean-library»
import «lean-example».Support

def main (args : List String) : IO Unit := do
  let hook ← IO.getEnv "AFAIRESI_LEAN4_HOOK"
  IO.println s!"{Proofs.answer}:{support}:{hook.getD "unloaded"}"
  for argument in args do
    IO.println argument
