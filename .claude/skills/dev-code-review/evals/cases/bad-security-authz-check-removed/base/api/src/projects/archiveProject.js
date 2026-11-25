const { findProject, saveProject } = require("./projectStore");
const { HttpError } = require("../lib/errors");

// Archives a Project. Only a Workspace admin may archive.
async function archiveProject(req) {
  const { member } = req;
  const project = await findProject(member.workspaceId, req.params.projectId);
  if (!project) {
    throw new HttpError(404, "project not found");
  }
  if (member.role !== "admin") {
    throw new HttpError(403, "only an admin can archive a project");
  }
  project.archived = true;
  project.archivedBy = member.id;
  await saveProject(project);
  return { ok: true, project };
}

module.exports = { archiveProject };
