'use strict';
// Wire sizes for the event stream. Kept in JS so the agent and the Python
// reader cannot drift: the reader's rrtrace/format.py must match these values.

globalThis.RRFridaLayouts = Object.freeze({
  batchBytes: 64 * 1024,
  eventHeaderBytes: 40,
  registerPayloadBytes: 40,
  phaseEnter: 1,
  phaseLeave: 2,
  phaseInstruction: 3,
});
