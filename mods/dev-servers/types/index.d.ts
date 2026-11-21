export type Server = {
  pid: number
  port: number
  process: string
  command: string
  cwd: string
  age: string
  // what runs, from the command path (vite, roughdraft), and the project its folder belongs to
  tool: string | null
  project: string | null
}

export type RecipeEntry = {
  name: string
  command: string
  cwd: string
  port: number
}

declare module 'claude-code' {
  interface PluginState {
    'dev-servers': { servers: Server[]; root: string | null; recipe: RecipeEntry[] }
  }
}
