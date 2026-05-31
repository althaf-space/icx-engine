import { UserService } from '../services/user.service';
import { PostService } from '../services/post.service';
import { UserResolver } from './user.resolver';
import { PostResolver } from './post.resolver';

const userService = new UserService();
const postService = new PostService();
const userResolver = new UserResolver(userService);
const postResolver = new PostResolver(postService);

export const resolvers = {
  Query: {
    users: (_root: any, _args: any) => userResolver.Query_users(_root, _args),
    user: (_root: any, args: any) => userResolver.Query_user(_root, args),
    posts: (_root: any) => postResolver.Query_posts(_root),
  },
  User: {
    posts: (user: any) => postResolver.User_posts(user),
  },
};
