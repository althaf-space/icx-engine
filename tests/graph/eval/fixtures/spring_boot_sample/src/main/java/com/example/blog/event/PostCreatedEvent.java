package com.example.blog.event;

import com.example.blog.entity.Post;

public class PostCreatedEvent {
    private final Post post;

    public PostCreatedEvent(Post post) {
        this.post = post;
    }

    public Post getPost() { return post; }
}
