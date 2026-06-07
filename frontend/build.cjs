const { execSync } = require("child_process");
process.chdir(__dirname);
try {
  const out = execSync("npx vite build", { encoding: "utf8", stdio: "pipe" });
  console.log(out);
} catch (e) {
  console.error(e.stderr || e.stdout || e.message);
  process.exit(1);
}
