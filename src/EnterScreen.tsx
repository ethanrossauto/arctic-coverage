/**
 * The screen a visitor arrives on, and the one they are returned to when they wander off.
 *
 * 🔑 ITS REAL JOB IS TO BE SOMETHING TO READ WHILE THE DATABASE GETS UP. The compute suspends
 * when nothing has queried it for five minutes and takes a measured 11.9 s to come back on
 * the first connection. That is intolerable in front of a map that is supposed to be live and
 * perfectly ordinary in front of a screen with a button on it, so the wake is started the
 * moment this mounts and the seconds spent reading are seconds already being spent.
 *
 * ⚠️ SO THE BUTTON IS NOT CEREMONY. Removing it and entering automatically would move the
 * whole wake back in front of the map, which is the behaviour this replaced.
 *
 * 🔒 IT ALSO LIFTS THE BOOT CURTAIN. `index.html` covers the page until something says the
 * console is up, and for a first load that moment is now this screen appearing rather than
 * the map drawing: this needs no data, so it can be shown the instant the bundle parses, and
 * making the visitor watch "Loading..." before a button they still have to press would be two
 * waits where the design calls for one.
 */
import { useEffect } from "react";

import { useStore } from "./store";
import { wakeDatabase } from "./session";

export function EnterScreen({ about }: { about: string }) {
  const setPhase = useStore((s) => s.setPhase);

  useEffect(() => {
    // The earliest wake on a cold load is the inline one in `index.html`, which runs before
    // this bundle exists. This one covers every later return in the same page life.
    wakeDatabase();

    // Nothing here waits on the network, so the curtain has no reason to still be up.
    (window as { consoleReady?: () => void }).consoleReady?.();
  }, []);

  return (
    <div className="enter" role="dialog" aria-labelledby="enter-title">
      <div className="enter__inner">
        <div className="enter__mark" id="enter-title">
          ARCTIC COVERAGE
        </div>
        <p className="enter__about">{about}</p>
        <button className="enter__go" type="button" onClick={() => setPhase("live")} autoFocus>
          Enter
        </button>
        {/*
          ⚠️ SAYING WHY, BECAUSE A DELAY THAT IS EXPLAINED IS A DIFFERENT EXPERIENCE FROM THE
          SAME DELAY UNEXPLAINED. This is a demo running on a database that is allowed to go
          to sleep, which is a deliberate choice about cost rather than a fault, and a visitor
          who knows that reads a slow start as thrift instead of as a broken site.
        */}
        <p className="enter__note">
          The database sleeps when nobody is here, so the first load can take a few seconds.
        </p>
      </div>
    </div>
  );
}
