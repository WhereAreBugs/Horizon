import { handleRequest, type Env } from "../src/app";

export const onRequest: PagesFunction<Env> = (context) => {
  return handleRequest(context.request, context.env, context);
};
