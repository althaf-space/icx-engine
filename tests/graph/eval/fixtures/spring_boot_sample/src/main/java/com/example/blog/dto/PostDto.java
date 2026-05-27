package com.example.blog.dto;

import com.example.blog.entity.Post;

public class PostDto {
    public Long id;
    public Long authorId;
    public String title;
    public String body;
    public boolean published;

    public static PostDto from(Post post) {
        PostDto dto = new PostDto();
        dto.id = post.getId();
        dto.authorId = post.getAuthor() != null ? post.getAuthor().getId() : null;
        dto.title = post.getTitle();
        dto.body = post.getBody();
        dto.published = post.isPublished();
        return dto;
    }
}
