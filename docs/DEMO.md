# Demo guide

Everything the team needs for the live pitch: what to check before, what to click, what to say, and what to do if something breaks.

## Before you leave home

1. On the demo laptop, run `ollama list`. `gemma3:4b` must be in the list.
2. Double-click `selftest.bat`. Every line should say `ok`, and the model's "first decision" time should be printed. Note that number. It is how long each thinking step takes on stage.
3. Double-click `start_ayos.bat`, accept the administrator prompt, and wait until the right-hand panel says **Loaded and ready**.
4. Run each practice fault once at home, in this order: Fake proxy, Block example.com, Wrong DNS server, Adapter turned off. After each one, ask, press **Fix it**, and check the light goes green.
5. Copy `restore_network.bat` to the desktop. It is the panic button.
6. In Chrome or Edge, open Settings and search "secure DNS". Set it to off or to "current service provider". If it points to a named provider, the browser keeps working when Windows DNS is broken and the demo looks like nothing happened.
7. Plug in the charger at the venue. On battery the model runs slower.

## At the venue, before your slot

1. Connect to Wi-Fi or a phone hotspot and check the light says **Internet is working**.
2. Start Ayos and ask "what is DNS?" once. That first question loads the model into memory, so the real demo starts fast.
3. Press **Forget everything** only if you want to show memory from zero. Otherwise leave it.
4. Set the browser zoom so the back row can read it. 125% works on most projectors.
5. Close every other app. The model needs the memory.

## The five minutes

| Time | Do | Say |
|---|---|---|
| 0:00 | Show Ayos with the green light. | "When your internet dies, every AI assistant dies with it. You cannot ask a cloud AI to fix your internet, because it needs your internet. Ayos is a technician that lives on the laptop." |
| 0:30 | Press **Wrong DNS server**. Wait for the red light. Open a new browser tab and try any website. | "I just broke this laptop on purpose. Wi-Fi still says connected. No website opens. This is the call every family's tech person gets." |
| 1:00 | Type the problem in your own words and press **Check it**. Taglish is fine: "ayaw mag-load ng mga website pero connected naman yung wifi". | "The model is running on this laptop, offline. Watch it work. First it remembers what this PC looked like when the internet worked. Then it chooses a check, reads the result, and chooses again." |
| 1:45 | Point at the red dot on the path, then at the green safety line. | "It found the break: DNS. And here is the part we care about most. The model does not get the last word. Plain code checks that the results really show that cause. If the model guesses, it is refused." |
| 2:15 | Press **Fix it**. Wait for the Fixed stamp and the green light. Reload the website. | "It tells me exactly what it will change, and nothing happens until I approve. It fixed it, then ran the checks again to prove it." |
| 2:45 | Press **Undo this fix**, then **Put everything back**. Then press **Fake proxy**, ask again, fix. | "Every fix can be undone. And it learns this PC: it keeps a picture of healthy settings and a history of what broke, so it looks in the likely place first." |
| 3:30 | Point at the right-hand panel: model name, speed, "On this PC". Show the results table in the README if there is time. | "This is an open model running through Ollama on this CPU. The model picks from a fixed menu of eleven checks. It never writes a command. We tested the fixes on real Windows machines and measured the model on fourteen faults." |
| 4:15 | Back to the green light. | "Who is this for: families, small offices, computer shops, schools, anywhere the person who knows computers is not in the room. It is local because the thing it repairs is the connection itself." |

If you are short on time, cut the 2:45 row.

## If something goes wrong on stage

| What you see | What to do |
|---|---|
| The model is slow | Keep talking through the checks as they appear. That is the point of showing them. |
| "No local language model is running" | Ayos still diagnoses with its built-in rules and says so on screen. Say it out loud: "the model server stopped, and it fell back safely". Then open the Ollama app and ask again. |
| A fix says it needs administrator rights | Close Ayos and start it with `start_ayos.bat`. |
| The internet does not come back | Double-click `restore_network.bat` on the desktop. Wait 15 seconds for Wi-Fi. |
| The venue internet is down for real | Good news for the pitch. Ask Ayos. It should say the line itself is down and that nothing on the PC needs fixing. |
| Everything is on fire | Run `start_ayos_simulated.bat`. It is the same app on a pretend PC. Say clearly that it is the simulated PC. |

## Questions you will probably get

**Why do you need a language model if code checks the answer?**
The code can only confirm a cause. It cannot understand a person. The model reads "ayaw mag-load ng YouTube" and decides where to look, picks checks in a sensible order instead of running all of them, explains the result in the person's own language, and answers follow-up questions. The code is the inspector that keeps the model honest.

**What if the model is wrong?**
Then it is refused. A cause is accepted only when the check results show it. We measure this: the results table shows "model alone" next to "with safety check".

**Is it safe to give an AI administrator rights?**
The model has none. It can only name a check or a cause from a fixed list. The fixes are nine small functions we wrote, each one reversible, and none runs without a click.

**How is this different from the Windows troubleshooter?**
Microsoft is retiring the built-in troubleshooters. Ayos explains what it found in plain words, in your language, shows the evidence, remembers this PC, and can be asked questions.

**Does it really work offline?**
Yes. The model and all the logic are on the laptop. The only things that touch the network are the tests of the network.

**What did you build, and what is the open model?**
The model is one part, used as published. We built the agent loop, the menu of checks, the safety check, the fixes with undo, the memory, the Windows layer, the simulated PC, the evaluation tool and the interface.

**How fast is it?**
Give the number from your own `selftest.bat` run on the demo laptop. Do not quote a number you did not measure.

**What is next?**
More causes (printers, Bluetooth, drivers), a small installer, and a mode for computer shops that keeps a history per customer PC.
