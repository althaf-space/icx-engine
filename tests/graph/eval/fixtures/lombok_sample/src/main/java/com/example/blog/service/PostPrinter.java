package com.example.blog.service;

import com.example.blog.dto.PostDto;

public class PostPrinter {
    public String describe(PostDto post) {
        return post.getTitle() + " (published=" + post.isPublished() + ")";
    }

    public PostDto markPublished(PostDto post) {
        post.setPublished(true);
        return post;
    }
}
