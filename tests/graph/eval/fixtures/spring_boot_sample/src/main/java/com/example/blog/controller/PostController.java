package com.example.blog.controller;

import java.util.List;
import java.util.stream.Collectors;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.example.blog.dto.PostDto;
import com.example.blog.entity.Post;
import com.example.blog.service.PostService;

@RestController
@RequestMapping("/api/posts")
public class PostController {
    private final PostService postService;

    @Autowired
    public PostController(PostService postService) {
        this.postService = postService;
    }

    @GetMapping("/by-author/{authorId}")
    public List<PostDto> listByAuthor(@PathVariable Long authorId) {
        return postService.listForAuthor(authorId).stream()
                .map(PostDto::from)
                .collect(Collectors.toList());
    }

    @PostMapping("/by-author/{authorId}")
    public PostDto publish(@PathVariable Long authorId, @RequestBody PostDto payload) {
        Post post = postService.publish(authorId, payload.title, payload.body);
        return PostDto.from(post);
    }
}
