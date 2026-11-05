import Lean

open Lean

private partial def trivia (stx : Syntax) : Array Substring.Raw :=
  match stx with
  | .node _ kind children =>
    if kind == choiceKind then
      children[0]?.map trivia |>.getD #[]
    else
      children.foldl (fun result child => result ++ trivia child) #[]
  | .atom (.original leading _ trailing _) _
  | .ident (.original leading _ trailing _) .. => #[leading, trailing]
  | _ => #[]

private def stripTrivia (original : ByteArray) (result : ByteArray)
    (region : Substring.Raw) : ByteArray := Id.run do
  let mut result := result
  let mut cursor := region.startPos.byteIdx
  let stop := region.stopPos.byteIdx
  while cursor + 1 < stop do
    let start := cursor
    if original[cursor]! == 45 && original[cursor + 1]! == 45 then
      while cursor < stop && original[cursor]! != 10 do
        cursor := cursor + 1
    else if original[cursor]! == 47 && original[cursor + 1]! == 45 then
      cursor := cursor + 2
      let mut nesting := 1
      while cursor + 1 < stop && nesting > 0 do
        if original[cursor]! == 47 && original[cursor + 1]! == 45 then
          nesting := nesting + 1
          cursor := cursor + 2
        else if original[cursor]! == 45 && original[cursor + 1]! == 47 then
          nesting := nesting - 1
          cursor := cursor + 2
        else
          cursor := cursor + 1
    else
      cursor := cursor + 1
      continue
    let comment := String.fromUTF8! (original.extract start cursor)
    let preserve := ["lean-fmt:", "TODO", "FIXME", "HACK", "XXX", "NOSONAR", "~keep",
      "Copyright", "copyright", "SPDX-"].any (fun marker => (comment.splitOn marker).length > 1)
    unless preserve do
      for offset in [start:cursor] do
        unless original[offset]! == 10 || original[offset]! == 13 do
          result := result.set! offset 32
  return result

private def process (path : System.FilePath) : IO Unit := do
  let source := (← IO.FS.readFile path).crlfToLf
  let context := Parser.mkInputContext source path.toString
  let (header, parserState, messages) ← Parser.parseHeader context
  let (env, messages) ← Elab.processHeader header {} messages context
  let state ← Elab.IO.processCommands context parserState (Elab.Command.mkState env messages {})
  if state.commandState.messages.hasErrors then
    for message in state.commandState.messages.toList do
      IO.eprintln (← message.toString)
    throw (IO.userError s!"cannot remove comments from {path}")
  let original := source.toUTF8
  let regions := (#[header.raw] ++ state.commands).foldl
    (fun result stx => result ++ trivia stx) #[]
  let result := regions.foldl (stripTrivia original) original
  if result != original then
    IO.FS.writeBinFile path result

unsafe def main (arguments : List String) : IO Unit := do
  enableInitializersExecution
  initSearchPath (← findSysroot)
  for argument in arguments do
    process argument
