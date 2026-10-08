export type Server = {
  pid: number
  port: number
  process: string
  command: string
  cwd: string
  age: string
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
