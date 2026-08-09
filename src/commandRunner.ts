/**
 * A way for the map to send a command without owning one.
 *
 * 🔑 THE PROBLEM THIS SOLVES. Placing by hand needs two things that live in different
 * components: the position, which only the map has, and the sending, which only the command
 * bar can do. The bar owns the transcript, the busy flag and the audit trail, so a
 * placement that posted to the API directly would change the world with no record of it and
 * no line on screen, which is precisely what the log exists to prevent.
 *
 * ⚠️ IT WAS A STORE FIELD AND AN EFFECT FIRST, AND THAT WAS THE WRONG SHAPE. The map wrote
 * a request, the bar watched for it and ran it. That makes an effect do an event handler's
 * job: React's own lint rule catches it, and the reason behind the rule is real, since the
 * work is triggered by a click and not by anything rendering. A click handler calling a
 * function is the honest description of what happens, so that is what this is.
 *
 * 🔒 NULL WHEN NOTHING IS MOUNTED, AND CALLING IT THEN IS A NO-OP RATHER THAN A THROW. The
 * command bar registers on mount and clears on unmount, so the only window where this is
 * empty is one where there is no interface to have clicked anything in.
 */
export type CommandSource = "typed" | "voice" | "ui_button";

type Runner = (
  utterance: string,
  source: CommandSource,
  opts?: { parentCommandId?: string; plan?: unknown[]; heard?: string },
) => void;

let runner: Runner | null = null;

export function setCommandRunner(r: Runner | null): void {
  runner = r;
}

export function runCommand(...args: Parameters<Runner>): void {
  runner?.(...args);
}
