/* oxlint-disable max-depth -- Repeated argument values are grouped inside their action branch. */
/* oxlint-disable func-style, no-use-before-define, one-var, sort-keys, sort-vars -- Keep argument conversion together. */
/* oxlint-disable no-ternary, no-nested-ternary, no-undefined, unicorn/no-null, unicorn/no-nested-ternary -- Interpret optional CLI metadata. */
/* oxlint-disable max-lines-per-function, max-statements, complexity, no-magic-numbers -- Map argparse actions to controls and argv. */
/* oxlint-disable oxc/no-optional-chaining, oxc/no-rest-spread-properties -- Native ES modules target modern browsers. */

export function argumentMode(argument) {
  const action = argument.action || "store";
  if (Object.hasOwn(argument, "**kwargs")) {
    return "unsupported";
  }
  if (
    action === "BooleanOptionalAction" ||
    (typeof action === "object" && action.unknown?.endsWith("BooleanOptionalAction"))
  ) {
    return "boolean";
  }
  if (
    ["store_true", "store_false", "store_const", "append_const", "help", "version"].includes(action)
  ) {
    return "flag";
  }
  if (action === "count") {
    return "count";
  }
  if (
    !["store", "append", "extend"].includes(action) ||
    !argument.names.every((name) => typeof name === "string")
  ) {
    return "unsupported";
  }
  if (
    ["append", "extend"].includes(action) ||
    ["*", "+"].includes(argument.nargs) ||
    Number.isInteger(argument.nargs)
  ) {
    return "multiple";
  }
  if (argument.nargs && argument.nargs !== "?") {
    return "unsupported";
  }
  if (Array.isArray(argument.choices)) {
    return "choice";
  }
  return ["int", "float"].includes(argument.type) ? "number" : "text";
}

export function argumentRequired(argument) {
  return (
    argument.required === true ||
    (!argument.names[0].startsWith("-") && !["?", "*"].includes(argument.nargs))
  );
}

export function commandArguments(command, values) {
  const tokens = [],
    positional = [],
    active = new Set();
  for (const [index, argument] of (command.arguments || []).entries()) {
    const value = values[index] || {},
      mode = argumentMode(argument),
      required = argumentRequired(argument),
      flag = argument.names.find((name) => name.startsWith("--")) || argument.names[0],
      optional = flag.startsWith("-"),
      destination = optional ? tokens : positional;
    if (value.enabled && mode !== "unsupported") {
      active.add(index);
      if (mode === "flag") {
        destination.push(flag);
      } else if (mode === "boolean") {
        destination.push(value.text === "false" ? `--no-${flag.slice(2)}` : flag);
      } else if (mode === "count") {
        const count = Number(value.text);
        if (!Number.isInteger(count) || count < 1 || count > 100) {
          throw new Error(`${flag} needs a count from 1 to 100.`);
        }
        destination.push(...Array.from({ length: count }, () => flag));
      } else if (mode === "multiple") {
        const parts = String(value.text ?? "").split("\n"),
          size = Number.isInteger(argument.nargs) ? argument.nargs : 1,
          repeat = ["append", "extend"].includes(argument.action);
        if (
          (Number.isInteger(argument.nargs) &&
            (repeat ? parts.length % size !== 0 : parts.length !== size)) ||
          (argument.nargs === "+" && parts.length === 0)
        ) {
          throw new Error(`${flag} needs ${size} values${repeat ? " per occurrence" : ""}.`);
        }
        for (const part of parts) {
          validateValue(argument, part);
        }
        if (repeat && !["*", "+"].includes(argument.nargs)) {
          for (let offset = 0; offset < parts.length; offset += size) {
            const group = parts.slice(offset, offset + size);
            destination.push(...(optional ? [flag] : []), ...group);
          }
        } else {
          destination.push(...(optional ? [flag] : []), ...parts);
        }
      } else {
        const text = String(value.text ?? "");
        if (value.constant && argument.nargs === "?" && optional) {
          destination.push(flag);
        } else {
          validateValue(argument, text);
          destination.push(
            ...(optional
              ? [
                  flag.startsWith("--") ? `${flag}=${text}` : flag,
                  ...(flag.startsWith("--") ? [] : [text]),
                ]
              : [text]),
          );
        }
      }
    } else if (required && mode !== "unsupported") {
      throw new Error(`Provide ${flag}.`);
    }
  }
  for (const group of command.groups || []) {
    if (group.exclusive) {
      const selected = (command.arguments || []).filter(
        (argument, index) =>
          active.has(index) &&
          group.arguments.some((names) => names.some((name) => argument.names.includes(name))),
      );
      if (selected.length > 1 || (group.required && selected.length === 0)) {
        throw new Error(
          `Choose ${group.required ? "exactly" : "at most"} one of ${group.arguments.map((names) => names.join(" / ")).join(", ")}.`,
        );
      }
    }
  }
  return [...positional, ...tokens];
}

function validateValue(argument, value) {
  if (
    Array.isArray(argument.choices) &&
    !argument.choices.some((choice) => String(choice) === value)
  ) {
    throw new Error(`${argument.names[0]} must be one of ${argument.choices.join(", ")}.`);
  }
  if (
    ["int", "float"].includes(argument.type) &&
    (value.trim() === "" ||
      !Number.isFinite(Number(value)) ||
      (argument.type === "int" && !Number.isInteger(Number(value))))
  ) {
    throw new Error(
      `${argument.names[0]} needs ${argument.type === "int" ? "an integer" : "a number"}.`,
    );
  }
}
