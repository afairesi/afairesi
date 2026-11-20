import test_main
import «lean-example».Support

/-- The library exposes its answer. -/
theorem libraryAnswerIs42 : Proofs.answer = 42 := Proofs.answer_eq

/-- The suite can use a supporting module from another package. -/
theorem dependencyProvidesSupport : support = "resource" := rfl
