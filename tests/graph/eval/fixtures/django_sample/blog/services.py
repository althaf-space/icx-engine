from blog.models import Post


def publish_post(post: Post) -> Post:
    post.publish()
    return post


def post_word_count(post: Post) -> int:
    return len(post.body.split())
